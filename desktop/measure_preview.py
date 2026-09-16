"""Run the W04 acceptance against real media and print the evidence as JSON.

This is a *script*, not a test, for the same reason ``packaging/verify_member_package.py``
is: it drives a real window and a real sound device for several seconds, and the
numbers it produces are only meaningful next to the machine they were taken on.
The fast, deterministic checks live in ``tests/test_preview_*.py``; this is the
run that says what the preview actually costs here.

    & <work root>\\runtime\\venv\\Scripts\\python.exe desktop\\measure_preview.py
        --out <work root>\\out\\reports\\w04-preview.json

What it measures, and how it avoids fooling itself
--------------------------------------------------
* **First frame and seek latency** are wall-clock, measured around the session's
  own API, so they include decode start-up rather than hiding it.
* **"No old audio after a seek"** is decided by matching the PCM actually handed
  to the device against each word's own prepared audio.  A leaked block from the
  previous position therefore shows up as the *wrong word*, which is a number, not
  an opinion.
* **Resource release** is private bytes, handles and threads of this process plus
  the live decoder children, sampled before and after twenty open/play/seek/close
  cycles.  "Python GC is fine" is not evidence.
* **The decoder hang boundary** is exercised by running the real watchdog against
  a child that never produces a frame, and reporting the kill.

Not verified here: the target hardware.  This machine is a Ryzen 9 7940H with
32 GB and a discrete GPU; it is *not* the ordinary integrated-graphics
thin-and-light the requirement is written for, and the report says so.
"""
import argparse
from array import array
import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import struct
import subprocess
import sys
import time
import wave

CHECKOUT = Path(__file__).resolve().parents[1]
if str(CHECKOUT) not in sys.path:
    sys.path.insert(0, str(CHECKOUT))


def work_root():
    """The work root above this checkout (`.../word_video_flow`), by the same rule
    the packaging scripts use: a folder holding both ``runtime/`` and ``worktrees/``.
    ``CHECKOUT.parent`` is ``worktrees/``, not the root, which is an easy way to
    send fixtures and outputs to the wrong place.
    """
    for candidate in list(CHECKOUT.parents)[:4]:
        if (candidate / 'runtime').is_dir() and (candidate / 'worktrees').is_dir():
            return candidate
    return CHECKOUT.parent

RATE = 48000
WORDS = (151, 152, 153)
TICKS = 720000


# ---------------------------------------------------------------- machine facts
def machine_facts():
    """What this run was taken on, because a latency number without it is noise."""
    facts = {'python': sys.version.split()[0], 'processor': os.environ.get('PROCESSOR_IDENTIFIER'),
             'logical_processors': os.cpu_count()}
    try:
        result = subprocess.run(
            ['powershell', '-NoProfile', '-Command',
             'Get-CimInstance Win32_Processor | Select-Object -First 1 -ExpandProperty Name;'
             'Get-CimInstance Win32_ComputerSystem | Select-Object -First 1 '
             '-ExpandProperty TotalPhysicalMemory;'
             'Get-CimInstance Win32_VideoController | Select-Object -ExpandProperty Name'],
            capture_output=True, text=True, timeout=60)
        lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
        if lines:
            facts['cpu'] = lines[0]
        if len(lines) > 1 and lines[1].isdigit():
            facts['ram_gb'] = round(int(lines[1]) / 1024 ** 3, 1)
        facts['gpus'] = lines[2:]
    except (OSError, subprocess.SubprocessError):
        pass
    facts['target_machine'] = False
    facts['target_note'] = ('not the requirement\'s ordinary integrated-graphics '
                            'thin-and-light; treat these numbers as an upper bound')
    return facts


class Counters(ctypes.Structure):
    _fields_ = [('cb', wintypes.DWORD), ('PageFaultCount', wintypes.DWORD),
                ('PeakWorkingSetSize', ctypes.c_size_t), ('WorkingSetSize', ctypes.c_size_t),
                ('QuotaPeakPagedPoolUsage', ctypes.c_size_t),
                ('QuotaPagedPoolUsage', ctypes.c_size_t),
                ('QuotaPeakNonPagedPoolUsage', ctypes.c_size_t),
                ('QuotaNonPagedPoolUsage', ctypes.c_size_t),
                ('PagefileUsage', ctypes.c_size_t), ('PeakPagefileUsage', ctypes.c_size_t),
                ('PrivateUsage', ctypes.c_size_t)]


def process_counters():
    """Private bytes, handles and threads of this process, plus decoder children."""
    import threading
    out = {'threads': threading.active_count()}
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(0x0400 | 0x0010, False, os.getpid())
    if handle:
        data = Counters()
        data.cb = ctypes.sizeof(Counters)
        if ctypes.windll.psapi.GetProcessMemoryInfo(handle, ctypes.byref(data), data.cb):
            out['private_bytes'] = data.PrivateUsage
        kernel32.CloseHandle(handle)
    count = wintypes.DWORD()
    if kernel32.GetProcessHandleCount(kernel32.GetCurrentProcess(), ctypes.byref(count)):
        out['handles'] = count.value
    try:
        result = subprocess.run(
            ['powershell', '-NoProfile', '-Command',
             '(Get-CimInstance Win32_Process -Filter "ParentProcessId=%d").ProcessId'
             % os.getpid()], capture_output=True, text=True, timeout=60)
        out['children'] = [int(line) for line in result.stdout.split() if line.strip().isdigit()]
    except (OSError, subprocess.SubprocessError):
        out['children'] = []
    return out


# ---------------------------------------------------------------- real media
def same_length(path):
    with wave.open(str(path), 'rb') as handle:
        return handle.getnframes()


def read_window(path, offset_frames, frames):
    with wave.open(str(path), 'rb') as handle:
        handle.setpos(offset_frames)
        return handle.readframes(frames)


def normalised_match(pcm, candidate_path, offset_frames):
    """How well a written block matches one candidate word at a known alignment.

    A normalised inner product, so a leaked block from the previous position
    scores near zero against the word that should be playing and high against the
    one that should not - which is what turns "sounds right" into a number.
    """
    count = min(len(pcm) // 2, 24000)
    if count <= 0:
        return 0.0
    reference = read_window(candidate_path, offset_frames, count)
    count = min(count, len(reference) // 2)
    if count <= 0:
        return 0.0
    left = struct.unpack('<%dh' % count, pcm[:2 * count])
    right = struct.unpack('<%dh' % count, reference[:2 * count])
    dot = sum(a * b for a, b in zip(left, right))
    norm_left = sum(a * a for a in left) ** 0.5
    norm_right = sum(b * b for b in right) ** 0.5
    if norm_left == 0 or norm_right == 0:
        return 0.0
    return dot / (norm_left * norm_right)


def rms(pcm):
    count = len(pcm) // 2
    if not count:
        return 0.0
    values = struct.unpack('<%dh' % count, pcm[:2 * count])
    return (sum(value * value for value in values) / count) ** 0.5


def leading_trailing_silence(pcm):
    """Frames of exact-zero at each end of a block; the declared silence, measured."""
    count = len(pcm) // 2
    if not count:
        return 0, 0
    values = struct.unpack('<%dh' % count, pcm[:2 * count])
    lead = 0
    while lead < count and values[lead] == 0:
        lead += 1
    tail = 0
    while tail < count - lead and values[count - 1 - tail] == 0:
        tail += 1
    return lead, tail


def prepare_real_plan(fixture, workspace, speed=None, fps=30):
    """The production path: raw TTS -> prepared speech -> project -> solved plan.

    The split matters and is easy to get wrong: ``MediaInfo`` describes the *raw*
    source (that is the grid the model divides by ``speed`` to get the timeline
    length), while the file the preview plays is the *prepared* one, which is
    already at ``speed``.  Resolving the asset map to the raw file would play the
    words at the wrong length; applying speed a second time in the preview would
    be the double-speed defect the media rules forbid.
    """
    from word_video.application import instantiate
    from word_video.domain import MediaInfo, Project, Record, solve
    from word_video.domain.lesson import DEFAULT_LESSON_TEMPLATE
    from word_video.domain.timebase import TICKS_PER_SECOND
    from word_video.media import atomic_json, prepare_audio
    from word_video.media.streams import audio_sample_count

    request = json.loads(Path(fixture).read_text(encoding='utf-8-sig'))
    speed = speed or float((request.get('lesson') or {}).get('speed') or 1.0)
    items = [item for item in (request.get('provider') or {}).get('items', [])
             if item.get('index') in WORDS]
    words = {}
    for item in items:
        words.setdefault(item['index'], {})[item['role']] = item
    wordlist = Path(request['source']['path'])
    if not wordlist.is_file():
        local = work_root() / 'data' / 'wordlists' / wordlist.name
        wordlist = local if local.is_file() else wordlist
    # The list is one entry per line, ``word /phonetic/ meaning``, with no index
    # column: the request's 1-based ``range`` is a *line* range, so entry 151 is
    # line index 150.  That is why the fixture asks for 151..153 and gets
    # demonstrate/deputy/continuous.
    lines = [line for line in wordlist.read_text(encoding='utf-8-sig').splitlines()
             if line.strip()]
    entries = {}
    for number in WORDS:
        if number - 1 >= len(lines):
            raise SystemExit('word list has %d lines, entry %d asked for (%s)'
                             % (len(lines), number, wordlist))
        head, _, rest = lines[number - 1].strip().partition(' ')
        phonetic = ''
        meaning = rest.strip()
        if meaning.startswith('/') and '/' in meaning[1:]:
            phonetic, _, meaning = meaning[1:].partition('/')
            phonetic = '/' + phonetic + '/'
            meaning = meaning.strip()
        entries[number] = (head, phonetic, meaning)

    records, media, assets, prepared = [], {}, {}, {}
    for index in WORDS:
        word, phonetic, meaning = entries[index]
        record = Record(id='w%d' % index, word=word, phonetic=phonetic,
                        meaning=meaning, spoken_meaning=meaning, index=index)
        records.append(record)
        for role, item in sorted(words[index].items()):
            asset_id = '%s:%s' % (record.id, role)
            source = Path(item['path'])
            target = Path(workspace) / ('%s.%s.wav' % (record.id, role))
            if not target.is_file():
                prepare_audio(source, target, speed, fps)
            facts = audio_sample_count(source)
            media[asset_id] = MediaInfo(asset_id, facts['samples'], 1,
                                        facts['sample_rate'])
            assets[asset_id] = str(target)
            prepared[asset_id] = {'raw': str(source), 'prepared': str(target),
                                  'raw_samples': facts['samples'],
                                  'raw_rate': facts['sample_rate'],
                                  'prepared_frames': same_length(target)}
    lesson = request.get('lesson') or {}
    project = Project(project_id='w04-measure', width=int(lesson.get('width') or 1920),
                      height=int(lesson.get('height') or 1080),
                      speed=speed, intro_s=0.0, gap_s=0.2)
    background = lesson.get('background')
    background_slice = None
    if background and Path(background).is_file():
        from word_video.domain import MediaSlice
        frames = int(round(float(lesson.get('background_seconds') or 0) * RATE))
        background_slice = MediaSlice(asset_id='layer:background', source_start=0,
                                      source_end=max(1, int(30 * RATE)))
        assets['layer:background'] = background
    project = instantiate(DEFAULT_LESSON_TEMPLATE, tuple(records), media, project,
                          background=background_slice)
    plan = solve(project, media).render
    return {'plan': plan, 'assets': assets, 'prepared': prepared, 'speed': speed,
            'project': project,
            'audio_starts': {item.clip_id: item.start_ticks for item in plan.audio},
            # Clip ids are ``w151.female``; asset ids are ``w151:female``.  Looking
            # the mapping up from the plan instead of writing ids by hand is what
            # keeps the two apart.
            'clip_assets': {item.clip_id: item.source.asset_id for item in plan.audio}}



class RecordingOutput:
    """Wraps the real device and keeps every block with the generation it was for.

    The log deliberately survives ``start()``: a seek restarts the device, and the
    whole point of the no-old-audio check is to compare what was written *before*
    the seek with what was written *after* it.
    """

    def __init__(self, sink, rate=RATE):
        self.sink = sink
        self.rate = rate
        self.blocks = []
        self.generations = []
        self.session = None
        self.bytes_written = 0

    def reset_log(self):
        self.blocks.clear()
        self.generations.clear()
        self.bytes_written = 0

    def start(self):
        return self.sink.start()

    def write(self, pcm):
        accepted = self.sink.write(pcm)
        if accepted:
            generation = self.session.clock.generation if self.session else -1
            self.generations.append(generation)
            self.blocks.append((generation, pcm[:accepted]))
            self.bytes_written += accepted
        return accepted

    def free_frames(self):
        return self.sink.free_frames()

    def frames_played(self):
        return self.sink.frames_played()

    def flush(self):
        return self.sink.flush()

    def stop(self):
        return self.sink.stop()

    def close(self):
        return self.sink.close()


# ---------------------------------------------------------------- the run
def run(args):
    if args.offscreen:
        os.environ['QT_QPA_PLATFORM'] = 'offscreen'
    from PySide6 import QtWidgets
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    from desktop.audio_qt import QtAudioOutput
    from desktop.preview_canvas import PreviewCanvas
    from preview.clock import NullAudioOutput, frames_for_ticks
    from preview.session import PreviewSession, DEFAULT_CANVAS_HEIGHT
    from preview.sources import proxy_cache_dir

    evidence = {'started': time.strftime('%Y-%m-%d %H:%M:%S'), 'machine': machine_facts(),
                'display': {'platform': os.environ.get('QT_QPA_PLATFORM') or 'default',
                            'note': 'offscreen excludes the compositor/present cost of a '
                                    'real window, so the first-frame number is a lower '
                                    'bound for what a member sees'}}
    workspace = Path(args.workspace)
    workspace.mkdir(parents=True, exist_ok=True)
    built = prepare_real_plan(args.fixture, workspace)
    plan = built['plan']
    assets = built['assets']
    evidence['plan'] = {'words': len(WORDS), 'audio_clips': len(plan.audio),
                        'video_clips': len(plan.video), 'fps': plan.fps_num,
                        'size': '%dx%d' % (plan.width, plan.height),
                        'total_seconds': round(plan.total_ticks / TICKS, 3),
                        'speed': built['speed'],
                        'audio_starts_seconds': {
                            clip: round(ticks / TICKS, 3)
                            for clip, ticks in built['audio_starts'].items()}}
    evidence['prepared_audio'] = built['prepared']

    if QtAudioOutput.available():
        recording = RecordingOutput(QtAudioOutput(rate=RATE))
        evidence['audio_device'] = {'kind': 'QAudioSink', 'available': True}
    else:
        recording = RecordingOutput(NullAudioOutput(rate=RATE))
        evidence['audio_device'] = {'kind': 'null', 'available': False,
                                    'note': 'no output device; the picture path is still '
                                            'measured but latency to sound is not'}
    session = PreviewSession(plan, assets, output=recording, temp_root=Path(args.tmp),
                             canvas_height=DEFAULT_CANVAS_HEIGHT)
    recording.session = session

    window = QtWidgets.QWidget()
    window.resize(640, 360)
    box = QtWidgets.QVBoxLayout(window)
    canvas = PreviewCanvas(session)
    box.addWidget(canvas)
    window.show()
    canvas.start()
    app.processEvents()

    try:
        # (1) first frame, measured until the *canvas* has drawn it, so the number
        # includes the paint and not just the decode.
        recording.reset_log()
        started = time.monotonic()
        session.play()
        while (canvas.frames_drawn == 0 and time.monotonic() - started < args.first_frame_timeout):
            app.processEvents()
            time.sleep(0.002)
        first = time.monotonic() - started
        if canvas.frames_drawn == 0:
            raise SystemExit('no frame reached the canvas within %ss' % args.first_frame_timeout)
        evidence['first_frame'] = {
            'open_seconds': None if session.open_seconds is None
                            else round(session.open_seconds, 4),
            'play_to_canvas_seconds': round(first, 4),
            'engine_play_to_frame_seconds': None if session.first_frame_seconds is None
                                            else round(session.first_frame_seconds, 4),
            'canvas': '%dx%d' % (session.canvas_width, session.canvas_height),
            'sources': {key: value.to_dict() for key, value in session.sources.items()},
            'budget_assumption_seconds': 1.5,
            'met_budget': first < 1.5}

        # Let some of word 1 play so there is a real "old position" to leak.
        time.sleep(0.8)
        app.processEvents()
        before_blocks = len(recording.blocks)
        # Land a little way *inside* the third word.  The prepared TTS clips open
        # with a short silence (measured: 0.00 RMS for the first 100 ms), so a
        # comparison at the very start would be correlating silence with silence
        # and reporting 0.000 for both candidates - which looks exactly like a
        # failure while proving nothing.
        probe_frames = int(args.probe_offset * RATE)
        probe_ticks = int(args.probe_offset * TICKS)
        target = built['audio_starts']['w153.female'] + probe_ticks
        position_before = session.position_ticks()
        generation_before = session.clock.generation
        target_generation = generation_before + 1

        # (2) seek latency and the no-old-audio decision.
        seek_started = time.monotonic()
        session.seek(target)
        frames_before_seek = canvas.frames_drawn
        seek_frame = None
        seek_audio = None
        deadline = seek_started + 5.0
        while time.monotonic() < deadline:
            app.processEvents()
            if seek_frame is None and canvas.frames_drawn > frames_before_seek:
                seek_frame = time.monotonic() - seek_started
            if seek_audio is None and len(recording.blocks) > before_blocks:
                seek_audio = time.monotonic() - seek_started
            if seek_frame is not None and seek_audio is not None:
                break
            time.sleep(0.002)
        after = recording.blocks[before_blocks:]
        evidence['seek'] = {
            'from_seconds': round(position_before / TICKS, 3),
            'to_seconds': round(target / TICKS, 3),
            'generation_before': generation_before,
            'generation_after': session.clock.generation,
            'generation_is_new': session.clock.generation == target_generation,
            'frame_seconds': None if seek_frame is None else round(seek_frame, 4),
            'audio_seconds': None if seek_audio is None else round(seek_audio, 4),
            'blocks_after_seek': len(after),
            'generations_written_after_seek': sorted({gen for gen, _ in after}),
            'all_after_seek_are_new_generation': bool(after) and all(
                gen == target_generation for gen, _ in after),
            'budget_assumption_seconds': 0.3,
            'met_budget': seek_frame is not None and seek_frame < 0.3}
        if after:
            first_block = after[0][1]
            word3 = built['assets'][built['clip_assets']['w153.female']]
            word1 = built['assets'][built['clip_assets']['w151.female']]
            score_new = normalised_match(first_block, word3, probe_frames)
            score_old = normalised_match(first_block, word1, probe_frames)
            # The block handed over first must be the audio *at the new position*;
            # if the probe had landed in the clips' silent lead-in both scores
            # would be 0.000, which is why the reference level is reported too.
            expected = read_window(word3, probe_frames, min(len(first_block) // 2, 24000))
            evidence['no_old_audio'] = {
                'probe_offset_frames': probe_frames,
                'probe_offset_seconds': round(probe_frames / RATE, 4),
                'reference_rms_new': round(rms(read_window(word3, probe_frames, 24000)), 2),
                'reference_rms_old': round(rms(read_window(word1, probe_frames, 24000)), 2),
                'block_rms': round(rms(first_block), 2),
                'block_matches_new_sample_for_sample': first_block == expected,
                'matched_new_position': round(score_new, 4),
                'matched_old_position': round(score_old, 4),
                'decided_by': "normalised match of the PCM handed to the device against "
                              "each word's own prepared audio, a little way inside the "
                              "word so silence is not compared with silence",
                'verdict': 'new position' if score_new > score_old + 0.1 else 'LEAK'}
        session.pause()
    finally:
        # The session must be closed whatever happened above: an unclosed session
        # leaves its mixer thread reading a device the garbage collector is about
        # to delete, which turns a measurement error into a confusing crash inside
        # a worker thread.
        canvas.stop()
        window.close()
        app.processEvents()
        session.close()

    # (3) the three mixing situations, checked on the PCM the mixer produces.
    evidence['mixing'] = mixing_cases(built, workspace)

    # (4) twenty open/play/seek/close cycles, with resource counters around them.
    evidence['resources'] = resource_cycles(plan, assets, args, app, workspace)

    # (5) the decoder hang boundary, run for real.
    evidence['decode_hang'] = hang_boundary(workspace)

    cache = proxy_cache_dir(Path(args.tmp))
    evidence['proxy_cache'] = {'dir': str(cache),
                               'files': len(list(cache.glob('*.mp4'))) if cache.is_dir() else 0}
    evidence['finished'] = time.strftime('%Y-%m-%d %H:%M:%S')
    return evidence


def mixing_cases(built, workspace):
    """Overlap, silence and a looping background, decided from rendered PCM and
    from frames that actually arrived."""
    from preview.audio import AudioSpan, WindowMixer
    from preview.clock import frames_for_ticks
    from preview.session import PreviewSession, picture_item, source_frames
    from word_video.media.streams import video_stream

    plan = built['plan']
    assets = built['assets']
    result = {}

    spans = []
    for item in plan.audio:
        first = frames_for_ticks(item.start_ticks, RATE)
        last = frames_for_ticks(item.end_ticks, RATE)
        spans.append(AudioSpan(path=assets[item.source.asset_id], first_frame=first,
                               last_frame=last, label=item.role))

    # -- silence: where the project declares it, the preview must be silent.
    # The rhythm gives every stage a fixed length and the prepared speech is padded
    # to it, so on this fixture the silence lives at the *tail of a stage* rather
    # than in a gap between two stages.  Both shapes are reported, because a
    # preview that carried sound across either one would be describing a different
    # lesson.
    mixer = WindowMixer(spans)
    gaps = [(left.last_frame, right.first_frame)
            for left, right in zip(spans, spans[1:]) if right.first_frame > left.last_frame]
    result['silence'] = {
        'stage_gaps_seconds': [round((end - start) / RATE, 4) for start, end in gaps],
        'stage_gaps_all_silent': all(
            rms(mixer.render_window(start, end - start)) == 0.0 for start, end in gaps),
        'stages': []}
    for span in spans[:3] + spans[-1:]:
        window = mixer.render_window(span.first_frame, span.frames)
        lead, tail = leading_trailing_silence(window)
        result['silence']['stages'].append({
            'label': span.label, 'frames': span.frames,
            'leading_silence_ms': round(lead * 1000.0 / RATE, 1),
            'trailing_silence_ms': round(tail * 1000.0 / RATE, 1),
            'rms': round(rms(window), 2),
            'silence_is_exact_zero': (lead + tail) > 0 and
                                     rms(window[2 * lead:2 * (span.frames - tail)]) > 0})
    speech = mixer.render_window(spans[0].first_frame + 2400, 4800)
    result['speech_control'] = {'rms': round(rms(speech), 2), 'audible': rms(speech) > 100}
    mixer.close()

    # -- overlap: proved as an identity, not as "it sounded louder".
    # Two different words placed on the same window must equal the sample-wise
    # clamped sum of the two rendered separately - which is the delivery mixer's
    # arithmetic, so a preview cannot disagree with an export about a deliberate
    # bed under a word.
    under = AudioSpan(path=spans[2].path, first_frame=spans[0].first_frame,
                      last_frame=spans[0].first_frame
                      + (spans[2].last_frame - spans[2].first_frame), label='bed')
    both = WindowMixer([spans[0], under])
    one = WindowMixer([spans[0]])
    other = WindowMixer([under])
    window_start = spans[0].first_frame + 600
    count = 3000
    summed = both.render_window(window_start, count)
    left = one.render_window(window_start, count)
    right = other.render_window(window_start, count)
    expected = array('h', [
        max(-32768, min(32767, a + b))
        for a, b in zip(struct.unpack('<%dh' % count, left),
                        struct.unpack('<%dh' % count, right))])
    if sys.byteorder == 'big':
        expected.byteswap()
    result['overlap'] = {
        'window_frames': count,
        'rms_summed': round(rms(summed), 2),
        'rms_first_only': round(rms(left), 2),
        'rms_bed_only': round(rms(right), 2),
        'equals_clamped_sum_of_parts': summed == expected.tobytes(),
        'rule': 'two clips in one window add with s16 saturation, the same '
                'arithmetic word_video.media.audio.mix_clips uses for delivery'}
    both.close()
    one.close()
    other.close()

    # -- looping background: a source genuinely SHORTER than the item, decoded for
    # real, so the repeat is observed rather than assumed.
    result['looping_background'] = looping_background_case(workspace)
    # The delivered fixture's own background is longer than the lesson, which is a
    # different (and also correct) answer: no repeat is needed at all.
    item = picture_item(plan)
    if item is not None:
        entry = assets.get(item.source.asset_id)
        if entry is not None:
            try:
                seconds = float(video_stream(entry)['duration'])
            except (OSError, ValueError, KeyError):
                seconds = 0.0
            frames = source_frames(entry, plan.fps_num, plan.fps_den)
            tick = plan.fps_den * TICKS // plan.fps_num
            item_frames = (item.duration_ticks + tick - 1) // tick
            result['background_of_this_lesson'] = {
                'source_seconds': round(seconds, 4), 'source_frames': frames,
                'item_frames': item_frames,
                'repeat_needed': bool(frames and frames < item_frames),
                'note': 'the 200 s reference background is longer than the 11.25 s '
                        'lesson, so this fixture exercises the no-repeat path'}
    return result


def looping_background_case(workspace):
    """Decode a short background under a longer item and watch it actually repeat.

    Built here rather than taken from the fixture because the reference background
    is 200 s: it is *longer* than the lesson, so it never loops, and a preview that
    got looping wrong would still look perfect on it.
    """
    from preview.clock import NullAudioOutput
    from preview.session import PreviewSession
    from word_video.domain.model import MediaSlice
    from word_video.domain.plan import PlanItem, RenderPlan

    fps = 60
    source_seconds = 0.5
    source = write_short_video(Path(workspace) / 'short-background.mp4', source_seconds, fps)
    item_seconds = 2.0
    item = PlanItem(clip_id='layer.background', role='background', record_id='',
                    start_ticks=0, end_ticks=int(item_seconds * TICKS),
                    source=MediaSlice(asset_id='bg', source_start=0,
                                      source_end=int(source_seconds * RATE),
                                      unit_num=1, unit_den=RATE))
    plan = RenderPlan(project_id='loop-case', project_revision=0, fps_num=fps, fps_den=1,
                      width=320, height=180, sample_rate=RATE, channels=1,
                      total_ticks=int(item_seconds * TICKS), video=(item,), audio=())
    session = PreviewSession(plan, {'bg': str(source)}, proxy=False, canvas_height=180,
                             output=NullAudioOutput(rate=RATE))
    stamps = []
    try:
        session.open()
        session.play()
        deadline = time.monotonic() + item_seconds + 6.0
        last = None
        while time.monotonic() < deadline:
            presentation = session.snapshot()
            if presentation.frame is not None and presentation.frame.index != last:
                last = presentation.frame.index
                stamps.append(presentation.frame.pts_ticks)
            if session.clock.state in ('ended', 'released'):
                break
            time.sleep(0.005)
        stats = session.decoder.stats.to_dict() if session.decoder else {}
    finally:
        session.close()
    source_frames = int(round(source_seconds * fps))
    return {'source_frames': source_frames, 'item_frames': int(item_seconds * fps),
            'frames_presented': len(stamps),
            'passed_the_source_end': len(stamps) > source_frames,
            'timestamps_monotonic': stamps == sorted(stamps),
            'decoder_loops': stats.get('loops'),
            'rule': 'whole-file repeat in the same generation: the frame number keeps '
                    'counting, so timestamps stay monotonic - the same repeat '
                    'render._looped_background builds with the concat demuxer'}


def write_short_video(path, seconds, fps, size='320x180'):
    """A tiny real MP4 to loop; the harness may not import the test helpers."""
    from word_video.media.core import executable, run
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.is_file():
        run([executable('ffmpeg'), '-v', 'error', '-nostdin', '-n',
             '-f', 'lavfi', '-i', 'testsrc=size=%s:rate=%d:duration=%.4f'
             % (size, fps, seconds),
             '-pix_fmt', 'yuv420p', '-c:v', 'libx264', '-preset', 'ultrafast',
             '-movflags', '+faststart', str(path)])
    return path


def resource_cycles(plan, assets, args, app, workspace):
    """Twenty open/play/seek/close cycles; counters before and after."""
    from preview.session import PreviewSession, DEFAULT_CANVAS_HEIGHT

    baseline = process_counters()
    samples = []
    for index in range(20):
        session = PreviewSession(plan, assets, temp_root=Path(args.tmp),
                                 canvas_height=DEFAULT_CANVAS_HEIGHT)
        session.play()
        time.sleep(0.04)
        session.seek(plan.audio[index % len(plan.audio)].start_ticks)
        time.sleep(0.04)
        session.snapshot()
        app.processEvents()
        report = session.close()
        if index in (0, 4, 9, 19):
            samples.append({'cycle': index + 1, **process_counters(),
                            'threads_left': report['threads_left'],
                            'children_left': report['children_left'],
                            'mixer_stopped': report['mixer_stopped'],
                            'decoder_stopped': report['decoder_stopped']})
    # Let the OS reclaim the killed children before the final reading.
    time.sleep(0.5)
    after = process_counters()
    delta = {}
    for key in ('private_bytes', 'handles', 'threads'):
        if key in baseline and key in after:
            delta[key] = after[key] - baseline[key]
    delta['children'] = len(after.get('children', [])) - len(baseline.get('children', []))
    delta['private_mb'] = round(delta.get('private_bytes', 0) / 1024 ** 2, 2)
    return {'baseline': baseline, 'after': after, 'delta': delta, 'samples': samples,
            'cycles': 20,
            'verdict': ('bounded' if delta.get('threads', 0) <= 0
                        and delta.get('children', 0) <= 0
                        and delta.get('private_mb', 0) < 120 else 'LEAK-SUSPECT')}


def hang_boundary(workspace):
    """Run the shipping watchdog against a child that never produces a frame."""
    from preview.queues import DROP_OLDEST, BoundedQueue
    from preview.video import DecodeSpec, FfmpegFrameDecoder

    queue = BoundedQueue(maxlen=4, overflow=DROP_OLDEST)
    decoder = FfmpegFrameDecoder(queue,
                                 ffmpeg=[sys.executable, '-c',
                                         'import time; time.sleep(600)'],
                                 stall_timeout=0.5, max_recoveries=0)
    spec = DecodeSpec(path=str(Path(workspace) / 'unused.mp4'), width=320, height=180,
                      fps_num=30, fps_den=1, start_ticks=0, end_ticks=10 * TICKS,
                      source_frames=300)
    decoder.start()
    decoder.request(1, spec)
    started = time.monotonic()
    deadline = started + 20.0
    while decoder.stats.failures == 0 and time.monotonic() < deadline:
        time.sleep(0.05)
    detected = time.monotonic() - started
    stop_started = time.monotonic()
    stopped = decoder.stop(timeout=10.0)
    teardown = time.monotonic() - stop_started
    return {'detected_after_seconds': round(detected, 3), 'stall_timeout': 0.5,
            'stalls': decoder.stats.stalls, 'killed': decoder.stats.killed,
            'failures': decoder.stats.failures, 'frames': decoder.stats.frames,
            'failure_reason': decoder.stats.failure_reason,
            'stop_returned': stopped, 'teardown_seconds': round(teardown, 3),
            'threads_left': decoder.alive_threads(),
            'children_left': decoder.alive_children(),
            'how_to_reproduce': 'tests/test_preview_video.py::'
                                'test_a_wedged_decoder_is_killed_and_its_thread_ends',
            'recovery_written_down': (
                'A stall is "no frame for stall_timeout".  The decoder kills the '
                'child, which closes the pipe and unblocks the reader by itself; it '
                'then resumes from the frame after the last one presented, retrying '
                'max_recoveries times before reporting 无响应 and stopping.  The UI '
                'keeps its last picture and stays responsive because no thread is '
                'joined without a timeout.')}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--fixture', default=None, help='three-word request JSON')
    parser.add_argument('--workspace', default=None, help='scratch folder for prepared audio')
    parser.add_argument('--tmp', default=None, help='proxy cache root')
    parser.add_argument('--out', default=None, help='write the evidence JSON here')
    parser.add_argument('--first-frame-timeout', type=float, default=20.0)
    parser.add_argument('--probe-offset', type=float, default=0.3,
                        help='seconds inside the target word where the audio match is '
                             'decided; past the clips\' silent lead-in')
    parser.add_argument('--on-screen', action='store_true',
                        help='show a real window; the default is the offscreen platform '
                             'so a measurement run cannot steal focus from a member')
    args = parser.parse_args(argv)
    args.offscreen = not args.on_screen
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')

    root = work_root()
    args.fixture = args.fixture or str(root / 'data' / 'fixtures' / 'p1-有片头.json')
    args.tmp = args.tmp or str(root / 'runtime' / 'tmp' / 'C' / 'preview-cache')
    args.workspace = args.workspace or str(root / 'runtime' / 'tmp' / 'C' / 'measure')
    Path(args.tmp).mkdir(parents=True, exist_ok=True)
    Path(args.workspace).mkdir(parents=True, exist_ok=True)

    evidence = run(args)
    payload = json.dumps(evidence, ensure_ascii=False, indent=1)
    if args.out:
        Path(args.out).write_text(payload, encoding='utf-8')
    print(payload)
    return 0


if __name__ == '__main__':
    sys.exit(main())
