"""Create genuinely editable drafts without importing the Windows UI controller."""
import importlib
import importlib.metadata
import importlib.util
import json
import math
import os
from pathlib import Path
import shutil
import sys
import time
import types
import uuid

from .media import atomic_json, duration, executable, resolve_intro_audio, run
from .template import default_styles, text_events


def _has_audio(path):
    """Whether a clip carries a sound stream, so extraction can be skipped."""
    from .media import has_audio
    return has_audio(path)


def _load_library():
    # The upstream __init__ imports JianyingController on Windows. Load its pure
    # relative submodules in a dedicated namespace instead. No controller is loaded.
    package = '_word_video_jyd'
    if package not in sys.modules:
        dist = importlib.metadata.distribution('pyJianYingDraft')
        if dist.version != '0.3.0':
            raise RuntimeError('Expected pyJianYingDraft 0.3.0')
        path = Path(dist.locate_file('pyJianYingDraft'))
        mod = types.ModuleType(package)
        mod.__path__ = [str(path)]
        mod.__package__ = package
        sys.modules[package] = mod
    def load(name):
        return importlib.import_module(package + '.' + name)
    return types.SimpleNamespace(
        folder=load('draft_folder'), script=load('script_file'), track=load('track'),
        audio=load('audio_segment'), video=load('video_segment'),
        text=load('text_segment'), timer=load('time_util'), materials=load('local_materials'),
        keyframe=load('keyframe'), mix_mode=load('metadata.mix_mode_meta'))


def _overlong_speech(item, source, actual, planned_us, manifest):
    """Why one stage does not fit, in the terms the member can act on.

    "Speech exceeds allocated timeline stage" named neither the word, the role, the
    file nor the size of the problem, and the most common cause is not a broken
    file at all: a *raw* recording handed to ``prepared_speech`` has not been
    through the tempo pass, so it is longer than the stage reserved for it by
    exactly the speed factor.  The message therefore leads with the measurements
    and ends with the route that does apply the tempo (``provider.kind=local``).
    """
    planned_s = planned_us / 1e6
    frames = item['duration_frames']
    shortfall = actual - planned_s
    # The speed that would make it fit; a ready-made number beats a diagnosis.
    speed = actual / planned_s if planned_s > 0 else 0.0
    return ('word %s %s: speech is %.3fs but the timeline reserved %.3fs '
            '(%d frames at %s fps) - %.3fs too long, about %.2fx; '
            'file %s; this usually means the audio was not tempo-adjusted to the '
            'project speed (%.2fx). Reuse existing audio through '
            'provider.kind=local, which measures and re-times the original for '
            'you, instead of passing it as prepared_speech'
            % (item.get('word_index'), item['role'], actual, planned_s, frames,
               manifest.fps, shortfall, speed, source,
               getattr(manifest, 'speed', 0.0) or 0.0))


def export_draft(manifest, output_dir):
    """output_dir is a NEW draft folder, never an existing project."""
    target = Path(output_dir).resolve()
    if target.exists():
        raise FileExistsError(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    stage = target.parent / ('.' + target.name + '.building-' + uuid.uuid4().hex)
    lib = _load_library()
    folder = lib.folder.DraftFolder(str(target.parent))
    script = folder.create_draft(stage.name, manifest.width, manifest.height,
                                 manifest.fps, maintrack_adsorb=False)
    refs = {}
    for name, kind in [('背景', 'video'), ('片头', 'video'), ('female', 'audio'),
                       ('male', 'audio'), ('chinese', 'audio'), ('片头音效', 'audio')]:
        refs[name] = script.append_track(lib.track.TrackSpec(getattr(lib.track.TrackType, kind), name))
    for name in default_styles():
        refs[name] = script.append_track(lib.track.TrackSpec(lib.track.TrackType.text, name))
    us = lambda frame: (frame * 1000000 + manifest.fps // 2) // manifest.fps
    # Plan items are counted in ticks (720000/s); the draft is built in frames, so a
    # tick value is converted through frames before it becomes microseconds.  Doing
    # it in one step is what keeps a background piece's segment inside its material.
    frames_of = lambda ticks: (ticks * manifest.fps + 360000) // 720000
    copied = {}
    resources = stage / 'Resources'
    resources.mkdir()
    def material_file(source):
        source = Path(source).resolve(strict=True)
        if source not in copied:
            dest = resources / (uuid.uuid4().hex + source.suffix)
            shutil.copy2(source, dest)
            copied[source] = str(dest)
        return copied[source]
    try:
        bg = lib.materials.VideoMaterial(material_file(manifest.background))
        # The intro lives on its own track above the background.  Keeping it off
        # the background track is what lets the background start at frame 0,
        # exactly as the rendered MP4 draws it: a transparent countdown is
        # composited *over* a background that is already running.
        if manifest.intro_video and manifest.intro_frames > 0:
            intro_material = lib.materials.VideoMaterial(
                material_file(manifest.intro_video))
            length = min(us(manifest.intro_frames), intro_material.duration)
            segment = lib.video.VideoSegment(
                intro_material, lib.timer.Timerange(0, length))
            from .timing import intro_has_alpha
            if not intro_has_alpha(manifest.intro_video):
                # An opaque black-backed clip needs 变亮 (lighten) so the
                # background shows through.  A transparent export already
                # carries alpha, so it must stay a plain overlay or the blend
                # would be applied twice.  Written as an editable mix mode.
                segment.set_mix_mode(lib.mix_mode.MixModeType.变亮)
            script.add_segment(segment, refs['片头'])
        capacity = math.floor(bg.duration * manifest.fps / 1000000)
        if capacity < 1:
            raise ValueError('Background has no usable frame')
        pieces = list(getattr(manifest, 'background_segments', ()) or ())
        if pieces:
            # A background the member cut in two: one editable segment per planned
            # piece, on its own file and its own place on the one background track.
            # Each piece loops inside itself exactly as the whole background did.
            for piece in pieces:
                material = lib.materials.VideoMaterial(material_file(piece['path']))
                start = us(frames_of(piece['start_ticks']))
                wanted = us(frames_of(piece['end_ticks'])) - start
                # The piece file is built to the frame the plan asks for, so these
                # agree; a material that disagrees by up to a frame is a rounding
                # artifact of the container, and clamping to what is really there
                # keeps the draft editable.  More than that is a wrong file, and
                # silently drawing less of the background would be the defect this
                # whole path exists to avoid - so it fails, naming the piece.
                available = material.duration
                tolerance = 1000000 // manifest.fps
                if wanted > available + tolerance:
                    raise ValueError(
                        'background piece %s is %.3fs but the plan reserves %.3fs '
                        '(%.3fs missing); the piece file %s does not cover its stage'
                        % (piece.get('clip_id'), available / 1e6, wanted / 1e6,
                           (wanted - available) / 1e6, piece['path']))
                length = min(wanted, available)
                script.add_segment(lib.video.VideoSegment(
                    material, lib.timer.Timerange(start, length), volume=0), refs['背景'])
        else:
            start = 0
            while start < manifest.total_frames:
                end = min(start + capacity, manifest.total_frames)
                script.add_segment(lib.video.VideoSegment(bg, lib.timer.Timerange(us(start), us(end)-us(start)),
                                                           volume=0), refs['背景'])
                start = end
        for item in manifest.audio:
            start = us(item['start_frame'])
            planned = us(item['start_frame'] + item['duration_frames']) - start
            source = Path(item['path']).resolve(strict=True)
            actual = duration(source)
            if actual > planned/1e6 + 1/manifest.fps:
                raise ValueError(_overlong_speech(item, source, actual, planned, manifest))
            padded = resources / (uuid.uuid4().hex + '.wav')
            run([executable('ffmpeg'), '-v', 'error', '-nostdin', '-n', '-i', source,
                 '-af', f'apad,atrim=duration={planned/1e6:.9f}', '-ar', '48000',
                 '-ac', '1', '-c:a', 'pcm_s16le', padded])
            copied[padded] = str(padded)
            mat = lib.materials.AudioMaterial(str(padded))
            # MediaInfo rounds WAV duration to milliseconds; keep target time exact.
            if abs(mat.duration - planned) > 2000:
                raise ValueError(
                    'prepared audio for word %s/%s disagrees with the timeline: the '
                    'draft reports %.3fs where %d us (%.3fs) were allocated (%s); '
                    'the file is %s'
                    % (item.get('word_index'), item['role'], mat.duration / 1e6,
                       planned, planned / 1e6, source, padded))
            mat.duration = max(mat.duration, planned)
            script.add_segment(lib.audio.AudioSegment(mat, lib.timer.Timerange(start, planned)), refs[item['role']])
        # One 片头音效 segment, from the single resolver the renderer uses too.
        # This branch used to prefer the configured ``intro_audio`` while the
        # mixer preferred the clip's own soundtrack, so a lesson that set both
        # produced a draft and an MP4 with different intro sound.  A silent
        # countdown clip contributes no segment at all (before M0 the extraction
        # was attempted anyway and failed the whole batch).
        intro_sound = resolve_intro_audio(manifest.intro_video, None,
                                          manifest.intro_audio)
        if not intro_sound.silent and manifest.intro_frames > 0:
            sound = resources / (uuid.uuid4().hex + '.wav')
            run([executable('ffmpeg'), '-v', 'error', '-nostdin', '-n', '-i',
                 str(Path(intro_sound.path).resolve(strict=True)), '-vn', '-t',
                 '%.9f' % (manifest.intro_frames / manifest.fps), '-ar', '48000',
                 '-ac', '1', '-c:a', 'pcm_s16le', sound])
            copied[sound] = str(sound)
            mat = lib.materials.AudioMaterial(str(sound))
            length = min(mat.duration, us(manifest.intro_frames))
            script.add_segment(lib.audio.AudioSegment(mat, lib.timer.Timerange(0, length)),
                               refs['片头音效'])
        text_fonts = {}
        styles = {k:dict(v) for k,v in default_styles().items()}
        for key,value in manifest.styles.items(): styles[key].update(value)
        for name, text, start, end in text_events(manifest):
            if not text:
                continue
            style = styles[name]
            rgb = tuple(int(style['color'][i:i+2], 16)/255 for i in (1,3,5))
            segment = lib.text.TextSegment(text, lib.timer.Timerange(us(start), us(end)-us(start)),
                style=lib.text.TextStyle(size=style['draft_size'], bold=style['bold'], color=rgb,
                                         align=1, auto_wrapping=True, max_line_width=.9),
                clip_settings=lib.video.ClipSettings(transform_x=style['x']*2-1, transform_y=1-style['y']*2),
                shadow=lib.text.TextShadow(alpha=.2, diffuse=15, distance=5, angle=-45))
            if name=='countdown' and style.get('animation')=='candidate_pulse':
                # Candidate pulse only; original encrypted preset is not yet identified.
                for prop,a,b in [(lib.keyframe.KeyframeProperty.uniform_scale,1.25,1),
                                  (lib.keyframe.KeyframeProperty.alpha,0,1)]:
                    segment.add_keyframe(prop,0,a)
                    segment.add_keyframe(prop,min(150000,us(end)-us(start)),b)
            script.add_segment(segment, refs[name])
            text_fonts[segment.material_id] = material_file(style['font'])
        content = json.loads(script.dumps())
        for text in content['materials']['texts']:
            font = text_fonts[text['id']]
            text['font_path'] = font
            value = json.loads(text['content'])
            for style in value['styles']:
                style['font'] = {'id': '', 'path': font}
            text['content'] = json.dumps(value, ensure_ascii=False)
        # Rewrite our staging paths to final paths before publication.
        def rewrite(value):
            if isinstance(value, str):
                if value.startswith('{'):
                    try:
                        return json.dumps(rewrite(json.loads(value)), ensure_ascii=False)
                    except json.JSONDecodeError:
                        pass
                return value.replace(str(stage), str(target)).replace(stage.as_posix(), target.as_posix())
            if isinstance(value, list):
                return [rewrite(x) for x in value]
            if isinstance(value, dict):
                return {k: rewrite(v) for k,v in value.items()}
            return value
        content = rewrite(content)
        content['name'] = target.name
        content['id'] = uuid.uuid4().hex.upper()
        atomic_json(stage / 'draft_content.json', content)
        meta = json.loads((stage/'draft_meta_info.json').read_text(encoding='utf-8'))
        meta.update(draft_id=content['id'], draft_name=target.name,
                    draft_fold_path=target.as_posix(), draft_root_path=target.parent.as_posix(),
                    tm_duration=us(manifest.total_frames), tm_draft_create=int(time.time()*1e6),
                    tm_draft_modified=int(time.time()*1e6))
        atomic_json(stage/'draft_meta_info.json', meta)
        atomic_json(stage/'word_video_manifest.json', manifest.to_dict())
        target_relative = {str(target / 'Resources' / Path(v).name): 'Resources/' + Path(v).name
                           for v in copied.values()}
        atomic_json(stage/'word_video_relocation.json', target_relative)
        os.rename(stage, target)
        return [str(target/'draft_content.json'), str(target/'draft_meta_info.json')]
    except Exception:
        # Only delete this invocation's unpublished staging folder.
        shutil.rmtree(stage)
        raise


class DraftEncryptedError(ValueError):
    """The app rewrote the draft as an opaque payload after opening it."""


class DraftCorruptError(ValueError):
    """The draft is neither valid plain JSON nor a recognised encrypted payload."""


def _read_draft_json(path):
    """Read an editable draft file, refusing an app-encrypted or corrupt one.

    Jianying 6.0+ (verified with 11.4.2) rewrites ``draft_content.json`` as an
    opaque payload once it saves the draft itself.  Rewriting it blind would
    destroy the user's edited project, so refuse and say so plainly.  Nothing is
    written before this check.
    """
    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError(path)
    raw = path.read_text(encoding='utf-8')
    if raw.lstrip()[:1] != '{':
        raise DraftEncryptedError(
            'Draft is encrypted by Jianying after it was opened; refusing to rewrite '
            '(the draft itself is still editable inside Jianying): ' + path.name)
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        raise DraftCorruptError('Draft is corrupted; refusing to rewrite: '
                                + path.name) from None


def relocate_draft(folder):
    root = Path(folder).resolve(strict=True)
    mapping = json.loads((root/'word_video_relocation.json').read_text(encoding='utf-8'))
    replacements = {}
    for old, relative in mapping.items():
        new = (root / relative).resolve(strict=True)
        if not new.is_relative_to(root):
            raise ValueError('Invalid relocation target')
        replacements[old] = str(new)
        replacements[old.replace('\\', '/')] = new.as_posix()
    content_path = root/'draft_content.json'
    content = _read_draft_json(content_path)
    def replace(value):
        if isinstance(value, str):
            if value in replacements:
                return replacements[value]
            # Font styles have a nested JSON string.
            if value.startswith('{'):
                try:
                    return json.dumps(replace(json.loads(value)), ensure_ascii=False)
                except json.JSONDecodeError:
                    pass
            return value
        if isinstance(value, list): return [replace(v) for v in value]
        if isinstance(value, dict): return {k: replace(v) for k,v in value.items()}
        return value
    backup = root/'draft_content.before-relocation.json'
    if backup.exists(): raise FileExistsError(backup)
    shutil.copy2(content_path, backup)
    atomic_json(content_path, replace(content))
    meta = json.loads((root/'draft_meta_info.json').read_text(encoding='utf-8'))
    meta.update(draft_fold_path=root.as_posix(), draft_root_path=root.parent.as_posix())
    atomic_json(root/'draft_meta_info.json', meta)
    atomic_json(root/'word_video_relocation.json', {replacements[k]:v for k,v in mapping.items()})


def draft_state(folder):
    """Report whether a draft can still be relocated, without modifying it.

    A draft opened and saved by Jianying 11.4 is encrypted; it stays fully
    editable inside Jianying but can no longer be patched from code.
    """
    root = Path(folder).resolve(strict=True)
    content = root/'draft_content.json'
    try:
        _read_draft_json(content)
    except FileNotFoundError:
        return {'draft': str(root), 'state': 'MISSING', 'relocatable': False,
                'editable_in_jianying': False}
    except DraftEncryptedError as error:
        return {'draft': str(root), 'state': 'ENCRYPTED_BY_APP', 'relocatable': False,
                'editable_in_jianying': True, 'note': str(error)}
    except DraftCorruptError as error:
        return {'draft': str(root), 'state': 'CORRUPT', 'relocatable': False,
                'editable_in_jianying': False, 'note': str(error)}
    return {'draft': str(root), 'state': 'PLAIN_JSON', 'relocatable': True,
            'editable_in_jianying': True,
            'mapping_present': (root/'word_video_relocation.json').is_file()}
