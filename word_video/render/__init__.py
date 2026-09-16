"""FFmpeg production renderer, independent of Jianying and all GUI automation.

The render is split into slices that run as separate processes.  libass
composites captions on a single thread, and measurement showed it is by far the
most expensive stage: decode 1332 fps, scale 1283 fps, intro composite 438 fps,
then 166 fps as soon as the captions are added, while the encoder costs only
~15% and neither the preset (p1/p4) nor the codec (hevc/h264) nor hardware
decode changes that.  One process therefore cannot use more than one core for
the captions, whatever encoder is chosen; eight slices measured 4.25x faster end
to end (word_video_work/bench_render*.json).
"""
import math
import os
from pathlib import Path
import queue
import shutil
import subprocess
import threading
import time
import uuid

from ..media import duration,executable,run
from ..template import default_styles
from ..validate import validate_video
from .ass import build_ass

MAX_SLICES = 8
# Below this a slice costs more to start than it saves: 10 s at 60 fps.
MIN_SLICE_FRAMES = 600


class RenderCancelled(Exception): pass


def video_graph(manifest, scale, intro_index=2):
    """Filter graph for a lesson that has a reference intro clip.

    The countdown is composited first and the captions are burnt in *last*, on
    the composited stream.  That ordering is the whole point of the function:
    drawing the captions on the background instead would lose every subtitle as
    soon as an intro was configured, and the intro could cover a word.
    """
    from ..timing import intro_has_alpha
    frames=max(1,min(manifest.intro_frames,manifest.total_frames))
    # Two reference exports exist and they need different compositing: a
    # transparent clip (bgra/rgba) is drawn with a plain alpha overlay; an
    # opaque black-backed clip must be lighten-blended, because both 'overlay'
    # (hides the background) and 'screen' (max per channel, washes the picture
    # out) are wrong for it.  The file decides.
    #
    # The composite is gated by frame number instead of trimming the streams: at
    # 4K an overlay works in ARGB (33 MB per frame) and a trim/concat pair
    # buffered enough frames to exhaust memory.  With `enable` nothing is
    # buffered and the countdown simply stops being drawn after the intro, which
    # is also exactly the intended cut.
    if intro_has_alpha(manifest.intro_video):
        head=(f'[0:v]{scale}[bg];[{intro_index}:v]{scale}[fg];'
              f'[bg][fg]overlay=0:0:enable=\'lt(n,{frames})\':format=auto[ov];')
    else:
        head=(f'[0:v]{scale}[bg];[{intro_index}:v]{scale}[fg];'
              f'[bg][fg]blend=all_mode=lighten:shortest=0:'
              f"enable='lt(n,{frames})'[ov];")
    return head+'[ov]ass=filename=captions.ass:fontsdir=fonts[vout]'


def slice_count(manifest):
    """How many processes one render is split into (1 keeps the single command)."""
    if manifest.total_frames < MIN_SLICE_FRAMES * 2:
        return 1
    return max(1, min(MAX_SLICES, os.cpu_count() or 4,
                      manifest.total_frames // MIN_SLICE_FRAMES))


def slice_bounds(total_frames, slices):
    """Frame ranges, as equal as possible, each non-empty."""
    step = total_frames / slices
    bounds = []
    for index in range(slices):
        start = int(round(index * step))
        end = int(round((index + 1) * step))
        bounds.append((start, max(start + 1, end)))
    bounds[-1] = (bounds[-1][0], total_frames)
    return bounds


def _looped_background(manifest, stage):
    """A background long enough to seek into, so slices need no stream looping.

    ``-stream_loop -1`` combined with an input seek has murky semantics, and a
    slice that had to decode from frame 0 would serialise the whole render.  The
    loop is built with the concat demuxer rather than ``-stream_loop -c copy``:
    the latter restarts the timestamps at every repeat, so seeking into the
    result lands in the wrong place and later slices render the wrong frames.
    """
    source = Path(manifest.background).resolve(strict=True)
    needed = manifest.total_frames / manifest.fps
    have = duration(source)
    if have >= needed:
        return source
    target = stage / 'background-looped.mp4'
    repeats = max(2, math.ceil(needed / have) + 1)
    listing = stage / 'background-loop.txt'
    listing.write_text("file '%s'\n" % source.as_posix() * repeats, encoding='utf-8')
    run([executable('ffmpeg'), '-v', 'error', '-nostdin', '-n', '-f', 'concat',
         '-safe', '0', '-i', listing, '-t', '%.9f' % needed, '-c', 'copy', target],
        timeout=900)
    return target


def _encoder_args(manifest, codec):
    presets={'h264_nvenc':['-preset','p4','-cq','20'],
             'hevc_nvenc':['-preset','p4','-cq','22'],
             'libx265':['-preset','medium','-crf','23','-tag:v','hvc1'],
             'libx264':['-preset','fast','-crf','20','-threads','4']}
    return presets.get(codec,presets['libx264'])


def _render_slices(manifest, stage, slices, background, codec, fonts_ready, progress, check):
    """Render every slice in parallel and return the concat list."""
    fps = manifest.fps
    scale = (f'scale={manifest.width}:{manifest.height}:'
             f'force_original_aspect_ratio=increase,crop={manifest.width}:{manifest.height},setsar=1')
    encoder = _encoder_args(manifest, codec)
    bounds = slice_bounds(manifest.total_frames, slices)
    intro = Path(manifest.intro_video).resolve(strict=True) if manifest.intro_video else None
    commands, partials = [], []
    key = 'captions.ass'
    for index, (start, end) in enumerate(bounds):
        count = end - start
        # A slice gets its own captions, shifted back to the slice's zero, and
        # only the events that fall inside it: fewer events, less compositing.
        name = 'captions.%d.ass' % index
        (stage / name).write_text(
            build_ass(manifest, offset_frames=start, frame_limit=count), encoding='utf-8-sig')
        args = [executable('ffmpeg'), '-v', 'warning', '-nostdin', '-n',
                '-ss', '%.9f' % (start / fps), '-t', '%.9f' % (count / fps),
                '-i', str(background)]
        if index == 0 and intro is not None:
            args += ['-stream_loop', '-1', '-i', str(intro)]
            graph = video_graph(manifest, scale, intro_index=1)
            graph = graph.replace('filename=captions.ass', 'filename=' + name)
        else:
            graph = (f'[0:v]{scale},ass=filename={name}:fontsdir=fonts[vout]')
        out = stage / ('slice-%d.mp4' % index)
        partials.append(out)
        args += ['-filter_complex', graph, '-map', '[vout]',
                 '-frames:v', str(count), '-r', str(fps),
                 '-c:v', codec, '-pix_fmt', 'yuv420p', *encoder,
                 '-progress', 'pipe:1', '-nostats', out]
        commands.append(args)
    counters = [0] * slices
    lock = threading.Lock()
    logs = [(stage / ('render-%d.log' % index)).open('w', encoding='utf-8')
            for index in range(slices)]
    processes = []
    for index, args in enumerate(commands):
        processes.append(subprocess.Popen([str(a) for a in args], cwd=stage,
                                          stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                          stderr=logs[index], text=True, encoding='utf-8',
                                          errors='replace',
                                          creationflags=subprocess.CREATE_NO_WINDOW
                                          if os.name == 'nt' else 0))

    def reader(index, process):
        for line in process.stdout:
            line = line.strip()
            if not line.startswith('frame='):
                continue
            try:
                value = int(line.split('=', 1)[1])
            except ValueError:
                continue
            with lock:
                counters[index] = value
                total = sum(counters)
            if progress:
                progress({'stage': 'render', 'frame': total,
                          'fraction': min(1, total / manifest.total_frames)})

    readers = [threading.Thread(target=reader, args=(index, process), daemon=True)
               for index, process in enumerate(processes)]
    for thread in readers:
        thread.start()
    try:
        while any(process.poll() is None for process in processes):
            check()
            time.sleep(.2)
    finally:
        for process in processes:
            if process.poll() is None:
                process.terminate()
        for thread in readers:
            thread.join(timeout=5)
        for process in processes:
            if process.stdout:
                process.stdout.close()
        for stream in logs:
            stream.close()
    failed = [(index, process.returncode) for index, process in enumerate(processes)
              if process.returncode]
    if failed:
        raise RuntimeError('FFmpeg slice(s) failed %r; see render-*.log' % (failed,))
    listing = stage / 'slices.txt'
    listing.write_text(''.join("file '%s'\n" % path.name for path in partials),
                       encoding='utf-8')
    return listing


def render_video(manifest, output_dir, progress=None, cancel=None, slices=None):
    from .mix import build_mix
    from ..media import resolve_intro_audio
    target=Path(output_dir).resolve()
    final=target/'video.mp4'
    if final.exists(): raise FileExistsError(final)
    def check():
        if cancel and cancel(): raise RenderCancelled('Rendering cancelled')
    check()
    target.mkdir(parents=True,exist_ok=True)
    stage=target/('.render-'+uuid.uuid4().hex);stage.mkdir()
    log=target/'render.log'
    proc=None
    try:
        ass=stage/'captions.ass';ass.write_text(build_ass(manifest),encoding='utf-8-sig')
        fonts=stage/'fonts';fonts.mkdir()
        styles={k:dict(v) for k,v in default_styles().items()}
        for key,value in manifest.styles.items():styles[key].update(value)
        if any(not s.get('font') for s in styles.values()):
            raise ValueError('No usable font for a rendered style; run doctor')
        font_paths={Path(s['font']).resolve(strict=True) for s in styles.values()}
        for i,path in enumerate(font_paths): shutil.copy2(path,fonts/(str(i)+path.suffix))
        # Resolved once, before any caption work, so the intro's sound is the
        # same decision the draft exporter made from the same manifest.
        intro_sound=resolve_intro_audio(manifest.intro_video,None,manifest.intro_audio)
        mix=stage/'mix.wav';build_mix(manifest,mix,cancel,intro_audio=intro_sound.path)
        check()
        codec='h264_nvenc'
        try:
            run([executable('ffmpeg'),'-v','error','-nostdin','-f','lavfi','-i',
                 f'color=s={manifest.width}x{manifest.height}:r={manifest.fps}',
                 '-frames:v','2','-c:v',codec,'-f','null','-'],timeout=20)
        except RuntimeError: codec='libx264'
        if manifest.video_codec=='h265':
            # HEVC hardware encoders vary by driver; fall back to the software
            # one so asking for h265 never fails outright.
            codec='hevc_nvenc'
            try:
                run([executable('ffmpeg'),'-v','error','-nostdin','-f','lavfi','-i',
                     f'color=s={manifest.width}x{manifest.height}:r={manifest.fps}',
                     '-frames:v','2','-c:v',codec,'-f','null','-'],timeout=20)
            except RuntimeError: codec='libx265'
        check()
        partial=stage/'video.partial.mp4'
        width,height=manifest.width,manifest.height
        scale=f'scale={width}:{height}:force_original_aspect_ratio=increase,crop={width}:{height},setsar=1'
        count = slice_count(manifest) if slices is None else max(1,int(slices))
        if count > 1:
            if progress:
                progress({'stage':'render','frame':0,'fraction':0,
                          'slices':count,'codec':codec})
            background=_looped_background(manifest,stage)
            listing=_render_slices(manifest,stage,count,background,codec,fonts,progress,check)
            check()
            silent=stage/'video.silent.mp4'
            run([executable('ffmpeg'),'-v','warning','-nostdin','-n','-f','concat',
                 '-safe','0','-i',listing,'-c','copy',silent],timeout=900)
            run([executable('ffmpeg'),'-v','warning','-nostdin','-n','-i',silent,'-i',mix,
                 '-map','0:v:0','-map','1:a:0','-c:v','copy','-c:a','aac','-b:a','192k',
                 '-movflags','+faststart',
                 '-t',f'{manifest.total_frames/manifest.fps:.9f}',partial],timeout=900)
        else:
            if manifest.intro_video:
                graph=video_graph(manifest,scale)
                args=[executable('ffmpeg'),'-v','warning','-nostdin','-n','-stream_loop','-1',
                      '-i',str(Path(manifest.background).resolve(strict=True)),'-i',str(mix),
                      '-stream_loop','-1','-i',str(Path(manifest.intro_video).resolve(strict=True)),
                      '-filter_complex',graph,'-map','[vout]','-map','1:a:0',
                      '-frames:v',str(manifest.total_frames),
                      '-t',f'{manifest.total_frames/manifest.fps:.9f}','-r',str(manifest.fps),
                      '-c:v',codec,'-pix_fmt','yuv420p']
            else:
                args=[executable('ffmpeg'),'-v','warning','-nostdin','-n','-stream_loop','-1',
                      '-i',str(Path(manifest.background).resolve(strict=True)),'-i',str(mix),
                      '-vf',f'{scale},ass=filename=captions.ass:fontsdir=fonts',
                      '-map','0:v:0','-map','1:a:0','-frames:v',str(manifest.total_frames),
                      '-t',f'{manifest.total_frames/manifest.fps:.9f}','-r',str(manifest.fps),
                      '-c:v',codec,'-pix_fmt','yuv420p']
            args+=_encoder_args(manifest,codec)
            args+=['-c:a','aac','-b:a','192k','-movflags','+faststart','-progress','pipe:1','-nostats',str(partial)]
            messages=queue.Queue()
            with log.open('w',encoding='utf-8') as errors:
                proc=subprocess.Popen(args,cwd=stage,stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,
                                      stderr=errors,text=True,encoding='utf-8',errors='replace',
                                      creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
                def read():
                    for line in proc.stdout: messages.put(line.strip())
                reader=threading.Thread(target=read,daemon=True);reader.start()
                while proc.poll() is None:
                    check()
                    try: line=messages.get(timeout=.2)
                    except queue.Empty: continue
                    if line.startswith('frame=') and progress:
                        frame=int(line.split('=',1)[1]);progress({'stage':'render','frame':frame,'fraction':min(1,frame/manifest.total_frames)})
                reader.join(timeout=2)
                proc.stdout.close()
                if proc.returncode: raise RuntimeError(f'FFmpeg failed ({proc.returncode}); see {log}')
        if count > 1:
            with log.open('w',encoding='utf-8') as errors:
                for index in range(count):
                    errors.write('# slice %d\n' % index)
                    piece=stage/('render-%d.log'%index)
                    if piece.exists(): errors.write(piece.read_text(encoding='utf-8',errors='replace'))
        check();validate_video(partial,manifest)
        # Final publication cannot overwrite a concurrently created user output.
        os.link(partial,final)
        shutil.copy2(ass,target/'captions.ass');shutil.copy2(mix,target/'mix.wav')
        if progress: progress({'stage':'generated','frame':manifest.total_frames,'fraction':1.0})
        return [str(final),str(target/'captions.ass'),str(target/'mix.wav'),str(log)]
    finally:
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try: proc.wait(timeout=5)
            except subprocess.TimeoutExpired: proc.kill();proc.wait(timeout=5)
            if proc.stdout: proc.stdout.close()
        shutil.rmtree(stage)
