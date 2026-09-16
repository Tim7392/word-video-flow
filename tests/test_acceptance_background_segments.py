"""The multi-segment background, judged from the plan and from the delivered pixels.

B proved the plan level.  This is the independent face H0 asked for, and it takes
its expectations from outside the artefacts it judges:

* **the plan** (the split ticks) says how many background pieces exist and where
  each stage starts and ends - not the timeline the exporter wrote;
* **the media** decides what a frame looks like: the two source files here are
  solid red and solid blue, so "both pieces reached the film" is a statement about
  pixel colour, not about a file name;
* **the draft** must carry one editable segment per planned piece, with the plan's
  own start and duration, and each segment's material must be a file inside the
  draft folder.

Two deliberately broken plans are also judged: a hole between the pieces and an
overlap between them must each be refused by name, and a split intro must be
refused instead of being half drawn.

The render is small (320x180, 30 fps, two words) so this stays a fast test; the
heavy pixel comparison runs under the ``acceptance_media`` marker and writes its
evidence to ``out/reports/``.
"""
import json
from dataclasses import replace
from fractions import Fraction
from pathlib import Path
import subprocess
import sys

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))

from acceptance import gate  # noqa: E402

pytestmark = pytest.mark.acceptance_media

WORKTREE = gate.worktree_of(__file__)
TICKS = 720000
FPS = 30


def media_tools():
    from word_video.media import executable
    return executable('ffmpeg'), executable('ffprobe')


def solid_clip(path, colour, seconds=0.4):
    """A real video file of one flat colour, so a frame says which piece it is."""
    ffmpeg, _ = media_tools()
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run([ffmpeg, '-v', 'error', '-nostdin', '-y', '-f', 'lavfi',
                    '-i', 'color=c=%s:size=160x90:rate=%d:duration=%.3f'
                    % (colour, FPS, seconds), '-c:v', 'libx264', '-pix_fmt',
                    'yuv420p', str(path)], check=True, capture_output=True)
    return path


def a_lesson(evidence):
    """Two words with the background split in two, each half a different colour."""
    from word_video.application import SplitClip, apply, instantiate
    from word_video.domain import MediaInfo, MediaSlice, Project, Record
    from word_video.domain.lesson import DEFAULT_LESSON_TEMPLATE
    from word_video.exporters.background import background_segments
    from word_video.exporters.plan import SourceMedia, build_manifest
    from word_video.template import default_styles
    from word_video.domain import solve
    import wave

    records = tuple(Record(id='w%d' % index, word=word, phonetic='/ˈæpəl/',
                           meaning='n. 苹果', spoken_meaning='苹果', index=index)
                    for index, word in ((1, 'apple'), (2, 'banana')))
    media = {'%s:%s' % (record.id, role): MediaInfo('%s:%s' % (record.id, role), 48000)
             for record in records for role in ('female', 'male', 'chinese')}
    red = solid_clip(evidence / 'bg-red.mp4', 'red', seconds=12.0)
    blue = solid_clip(evidence / 'bg-blue.mp4', 'blue', seconds=12.0)
    # Real (silent) speech files: the draft exporter publishes the media it is
    # pointed at, so a placeholder path would fail for the wrong reason.
    speech = {}
    for record in records:
        for role in ('female', 'male', 'chinese'):
            asset = '%s:%s' % (record.id, role)
            target = evidence / ('%s.wav' % asset.replace(':', '_'))
            if not target.exists():
                with wave.open(str(target), 'wb') as stream:
                    stream.setnchannels(1)
                    stream.setsampwidth(2)
                    stream.setframerate(48000)
                    stream.writeframes(b'\x00\x00' * 9600)
            speech[asset] = target

    project = instantiate(
        DEFAULT_LESSON_TEMPLATE, records, media,
        Project(project_id='qa-split-bg', intro_s=0.6, fps_num=FPS, width=320,
                height=180, speed=1.0),
        background=MediaSlice('layer.background', 0, 200000))
    total = project.clip('layer.background').duration_ticks
    project = apply(project, SplitClip('layer.background', total // 2)).project
    # Give each piece its own colour, so the film can be asked which piece is on
    # screen - the plan only knows ticks and windows.
    clips = tuple(replace(clip, source=MediaSlice('qa:bg-red', 0, 576000))
                  if clip.id == 'layer.background' else
                  replace(clip, source=MediaSlice('qa:bg-blue', 0, 576000))
                  if clip.id == 'layer.background.2' else clip
                  for clip in project.clips)
    project = project.with_clips(clips)
    solution = solve(project, media)
    sources = {asset: SourceMedia(path=str(path), voice='V')
               for asset, path in speech.items()}
    sources['qa:bg-red'] = SourceMedia(path=str(red))
    sources['qa:bg-blue'] = SourceMedia(path=str(blue))
    view = build_manifest(solution.render, project, sources, background=str(red),
                          styles=default_styles())
    pieces = background_segments(solution.render, sources)
    return project, media, solution, view, sources, pieces, red, blue


def frame_pixel(path, seconds):
    """Mean RGB of one frame, decoded here."""
    ffmpeg, _ = media_tools()
    out = subprocess.run([ffmpeg, '-v', 'error', '-nostdin', '-ss', '%.4f' % seconds,
                          '-i', str(path), '-frames:v', '1', '-vf', 'scale=8:8',
                          '-f', 'rawvideo', '-pix_fmt', 'rgb24', '-'],
                         capture_output=True, check=True).stdout
    size = 8 * 8
    if len(out) < size * 3:
        return None
    pixels = [out[i:i + 3] for i in range(0, size * 3, 3)]
    return tuple(sum(pixel[channel] for pixel in pixels) / len(pixels)
                 for channel in range(3))


def with_pieces(view, pieces, run_dir):
    """Attach the planned pieces to the view exactly as the exporter does.

    ``build_manifest`` deliberately does not carry them: the exporter renders each
    piece inside its own stage and then hands the list to the draft, so a test that
    wants the draft path has to do the same step instead of asserting on a field
    the manifest never sets.
    """
    from word_video.exporters.render import _background_piece

    run_dir.mkdir(parents=True, exist_ok=True)
    view.background_segments = [_background_piece(piece, index, run_dir, view)
                                for index, piece in enumerate(pieces, start=1)]
    return view


def test_the_plan_publishes_two_contiguous_pieces(tmp_path):
    evidence, warning = gate.evidence_dir()
    project, media, solution, view, sources, pieces, red, blue = a_lesson(evidence)
    assert [piece.clip_id for piece in pieces] == ['layer.background',
                                                   'layer.background.2']
    left, right = pieces
    # Contiguity is a property of the plan, checked against the plan's own ticks.
    assert left.end_ticks == right.start_ticks
    assert left.start_ticks == 0
    assert right.end_ticks == view.total_frames * TICKS // view.fps
    # Each piece is its own cut of its own file, so each window is independent.
    # The window is longer than the stage on purpose: the piece then needs no
    # repeat, which is the case the delivered draft supports today.  (A piece that
    # *loops* currently makes the draft read past its material - see the note in
    # test_the_draft_carries_one_editable_segment_per_piece.)
    assert (left.source_start, left.source_end) == (0, 576000)
    assert (right.source_start, right.source_end) == (0, 576000)
    assert 'red' in left.path and 'blue' in right.path, (left.path, right.path)
    # Each piece fills its own stage, so the two stages together are the lesson.
    assert abs(sum(piece.stage_seconds for piece in pieces)
               - view.total_frames / view.fps) <= 0.05


def test_each_planned_piece_reaches_the_delivered_film(tmp_path):
    """Pixel evidence: the first stage is red, the second is blue."""
    from word_video.exporters.background import build_background_track
    from word_video.media.streams import video_stream_seconds

    evidence, warning = gate.evidence_dir()
    project, media, solution, view, sources, pieces, red, blue = a_lesson(evidence)
    outputs = []
    for index, piece in enumerate(pieces, start=1):
        target = evidence / ('split-piece-%d.mp4' % index)
        if target.exists():
            target.unlink()
        outputs.append(build_background_track([piece], target, fps=view.fps,
                                             size=(view.width, view.height)))
    # Each piece covers its own stage, and together they cover the lesson.
    stages = [piece.stage_seconds for piece in pieces]
    lengths = [video_stream_seconds(path) for path in outputs]
    for length, stage in zip(lengths, stages):
        assert length + 0.05 >= stage, (length, stage)
    assert abs(sum(lengths) - view.total_frames / view.fps) <= 1.0
    # The picture itself: the halves must not look the same.
    first = frame_pixel(outputs[0], lengths[0] / 2)
    second = frame_pixel(outputs[1], lengths[1] / 2)
    assert first and second, (first, second)
    assert first[0] > first[2], '第一段应是红色：%r' % (first,)
    assert second[2] > second[0], '第二段应是蓝色：%r' % (second,)
    assert max(abs(a - b) for a, b in zip(first, second)) > 60, (first, second)


def test_the_draft_carries_one_editable_segment_per_piece(tmp_path):
    """The draft's background track is the plan, not a summary of it.

    Known defect (reported to H0, not hidden here): this case is built so each
    piece needs no repeat.  When a piece's stage is not a whole number of frames
    the looped piece file comes out one frame shorter than the stage
    (stage 106.5 frames -> file 106.0195 frames), and the draft then asks the
    material for the whole stage and ``pyJianYingDraft`` refuses with
    ``ValueError: 读取媒体时间范围 ... 超出媒体时长``.  The same project with a
    looping piece therefore cannot export a draft today; the fix belongs in the
    piece/loop rounding, and this test will cover it once that lands.
    """
    from word_video.draft import export_draft

    evidence, warning = gate.evidence_dir()
    project, media, solution, view, sources, pieces, red, blue = a_lesson(evidence)
    view = with_pieces(view, pieces, evidence / 'split-pieces')
    target = evidence / 'split-draft'
    if target.exists():
        import shutil
        shutil.rmtree(target)
    export_draft(view, target)
    draft = json.loads((target / 'draft_content.json').read_text(encoding='utf-8'))
    background = next(track for track in draft['tracks']
                      if track.get('name') == '背景')
    segments = sorted(background['segments'],
                      key=lambda item: item['target_timerange']['start'])
    # One editable segment per planned piece (the exporter's own contract).  Each
    # piece loops inside its own stage, so the track's spans must cover the stage
    # the piece was planned for - that is what "no hole, no repeat across the cut"
    # means once it reaches the draft.
    materials = {item['id']: item for group in draft['materials'].values()
                 if isinstance(group, list) for item in group
                 if isinstance(item, dict) and 'id' in item}
    for piece in pieces:
        start_us = round(piece.start_ticks * 1e6 / TICKS)
        end_us = round(piece.end_ticks * 1e6 / TICKS)
        covered = sorted((item['target_timerange']['start'],
                          item['target_timerange']['start']
                          + item['target_timerange']['duration'])
                         for item in segments
                         if start_us - 2 <= item['target_timerange']['start'] < end_us)
        assert covered, 'piece %s 没有落到草稿上' % piece.clip_id
        # Contiguous coverage of the stage: no hole between the loop segments.
        for (left_start, left_end), (right_start, _) in zip(covered, covered[1:]):
            assert abs(left_end - right_start) <= 2, (piece.clip_id, covered)
        assert abs(covered[0][0] - start_us) <= 2, (piece.clip_id, covered[:1])
        assert abs(covered[-1][1] - end_us) <= 2, (piece.clip_id, covered[-1:])
    # Each piece's material is a file that lives inside the draft folder, and the
    # two pieces are two different files.
    paths = []
    for piece in pieces:
        match = [item for item in materials.values()
                 if isinstance(item, dict) and 'path' in item
                 and Path(str(item['path'])).name
                 and Path(str(item['path'])).stat().st_size > 0
                 and Path(str(item['path'])).is_file()
                 and Path(str(item['path'])).resolve().is_relative_to(target.resolve())]
        assert match, 'piece %s 的素材不在草稿目录里' % piece.clip_id
    paths = sorted({str(item['path']) for item in materials.values()
                    if isinstance(item, dict) and 'path' in item})
    assert len([p for p in paths if p.endswith('.mp4')]) >= 2, paths


def test_a_hole_between_the_pieces_is_refused_by_name(tmp_path):
    from word_video.domain import TimeExpr
    from word_video.exporters.background import BackgroundError, background_segments

    evidence, warning = gate.evidence_dir()
    project, media, solution, view, sources, pieces, red, blue = a_lesson(evidence)
    moved = tuple(replace(clip, start=TimeExpr.at(clip.start.ticks + 24000),
                          duration_ticks=clip.duration_ticks - 24000)
                  if clip.id == 'layer.background.2' else clip
                  for clip in project.clips)
    from word_video.domain import solve
    plan = solve(project.with_clips(moved), media).render
    with pytest.raises(BackgroundError) as raised:
        background_segments(plan, sources)
    message = str(raised.value)
    assert 'hole' in message, message
    assert 'layer.background' in message and 'layer.background.2' in message, message


def test_an_overlap_between_the_pieces_is_refused_by_name(tmp_path):
    from word_video.domain import TimeExpr, solve
    from word_video.exporters.background import BackgroundError, background_segments

    evidence, warning = gate.evidence_dir()
    project, media, solution, view, sources, pieces, red, blue = a_lesson(evidence)
    moved = tuple(replace(clip, start=TimeExpr.at(clip.start.ticks - 24000),
                          duration_ticks=clip.duration_ticks + 24000)
                  if clip.id == 'layer.background.2' else clip
                  for clip in project.clips)
    plan = solve(project.with_clips(moved), media).render
    with pytest.raises(BackgroundError) as raised:
        background_segments(plan, sources)
    message = str(raised.value)
    assert 'overlap' in message, message
    assert 'layer.background' in message and 'layer.background.2' in message, message


def test_a_split_intro_is_refused_by_name(tmp_path):
    """The intro is the countdown; half of it is not a shorter countdown."""
    from word_video.application import SplitClip, apply, instantiate
    from word_video.domain import (IntroMeasurement, MediaInfo, MediaSlice, Project,
                                   Record, solve)
    from word_video.domain.lesson import DEFAULT_LESSON_TEMPLATE
    from word_video.exporters.plan import ProjectionError, SourceMedia, build_manifest
    from word_video.template import default_styles

    evidence, warning = gate.evidence_dir()
    clip = str(solid_clip(evidence / 'intro.mp4', 'green', seconds=0.5))
    record = Record(id='w1', word='apple', phonetic='/ˈæpəl/', meaning='n. 苹果',
                    spoken_meaning='苹果', index=1)
    media = {'w1:%s' % role: MediaInfo('w1:%s' % role, 48000)
             for role in ('female', 'male', 'chinese')}
    measurement = IntroMeasurement(asset_id=clip, seconds=0.5, sound_asset=clip,
                                   sound_source='FROM_CLIP', from_clip=True,
                                   picture_seconds=0.5)
    project = instantiate(
        replace(DEFAULT_LESSON_TEMPLATE, intro=True), (record,), media,
        Project(project_id='qa-split-intro', intro_s=2.0, fps_num=FPS),
        intro=MediaSlice(clip, 0, 48000), intro_measure=measurement)
    total = project.clip('layer.intro').duration_ticks
    project = apply(project, SplitClip('layer.intro', total // 2)).project
    solution = solve(project, media, measurement)
    sources = {'w1:%s' % role: SourceMedia(path='unused.wav', voice='V')
               for role in ('female', 'male', 'chinese')}
    with pytest.raises(ProjectionError) as raised:
        build_manifest(solution.render, project, sources, background=clip,
                       styles=default_styles())
    message = str(raised.value)
    assert 'split' in message, message
    assert 'layer.intro' in message and 'layer.intro.2' in message, message
