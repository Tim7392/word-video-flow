"""分段代理：网格无洞、边界帧正确、没准备好的区间是可见状态。

What these tests are for
------------------------
The segmented proxy exists because one 26 MB proxy of a 192.5 s lesson cost 66 s
before the first frame.  Speed is not a property a test can assert on a busy machine,
so what is asserted here is the set of invariants the speed must not be bought with:

* the grid **tiles** the playable frames with no gap and no overlap, including when the
  background loops (a segment never mixes two passes of the source);
* the grid is **the same for a longer lesson** - which is what lets an edit reuse the
  segments already on disk instead of invalidating all of them;
* a boundary frame is **the same picture** as the original at that instant (the
  preroll/tail are there for exactly this, and "the frame is missing" is the failure
  mode a segment proxy would otherwise have);
* a seek into a segment that is not encoded yet is a **visible state** - the session
  reports ``preparing`` and the canvas draws it - never a silent black frame;
* a source that segmentation does not apply to keeps the single-file proxy it had.
"""
import subprocess
import time
from pathlib import Path

import pytest

from preview.segments import (PREROLL_FRAMES, TAIL_FRAMES, SegmentError, SegmentPreparer,
                              build_segment_plan, source_frames)
from preview.session import PreviewSession, picture_item
from preview.video import DecodeSpec
from test_preview_support import (RATE, background_item, plan_of, scratch,
                                  three_tone_assets, write_test_video)
from word_video.domain.timebase import TICKS_PER_SECOND

FPS = 30
#: 720p source, 360p canvas: the smallest pair that still needs a proxy at all
#: (``preview_proxy_height(360)`` is the 540 preset, and a source at or below it is
#: never proxied), which keeps these tests quick without inventing a fake media layer.
SOURCE_SIZE = '1280x720'
CANVAS = 360
SOURCE_SECONDS = 3.0
COVERAGE_SECONDS = 3.0


@pytest.fixture(autouse=True)
def forget_resolutions():
    """The source-resolution cache is process-wide; each test starts from nothing."""
    from preview.sources import clear_resolution_cache
    clear_resolution_cache()
    yield
    clear_resolution_cache()


@pytest.fixture(scope='module')
def tall_source(tmp_path_factory):
    folder = tmp_path_factory.mktemp('segments-')
    return write_test_video(folder / 'background.mp4', seconds=SOURCE_SECONDS, fps=FPS,
                            size=SOURCE_SIZE)


def plan_for(source, *, coverage_seconds=COVERAGE_SECONDS, cache_dir,
             first_segment_seconds=1.0, segment_seconds=1.0, source_start=0, speed=1.0):
    frames = int(round(coverage_seconds * FPS))
    return build_segment_plan(source, cache_dir, CANVAS, FPS, 1, frames,
                              source_start=source_start, speed=speed,
                              first_segment_seconds=first_segment_seconds,
                              segment_seconds=segment_seconds)


def frame_signature(path, seconds):
    """A fixed-size grayscale signature of one frame, comparable across resolutions.

    Both sides are scaled to 64x36 first, so a 540p segment can be compared with the
    720p source (or with a proxy of it) without pretending two lossy encodes are
    byte-equal.  Sampling at the frame's *midpoint* keeps the sample unambiguous:
    sampling exactly on a boundary is a rounding decision, not a fact about the file.
    """
    from word_video.media.core import executable
    completed = subprocess.run(
        [executable('ffmpeg'), '-v', 'error', '-nostdin', '-n',
         '-ss', '%.6f' % seconds, '-i', str(path), '-frames:v', '1',
         '-vf', 'scale=64:36', '-f', 'rawvideo', '-pix_fmt', 'gray', '-'],
        capture_output=True, check=True)
    assert completed.stdout, 'no frame at %.4fs in %s' % (seconds, path)
    return completed.stdout


def signature_distance(one, two):
    """Mean absolute difference per pixel of two signatures (0 = same picture)."""
    return sum(abs(a - b) for a, b in zip(one, two)) / float(max(1, len(one)))


def test_the_grid_tiles_the_coverage_without_gaps_or_overlaps(tall_source):
    with scratch('segments-grid-') as folder:
        plan = plan_for(tall_source, cache_dir=folder)
        assert plan is not None
        assert [segment.first_frame for segment in plan.segments] == [0, 30, 60]
        assert [segment.cover for segment in plan.segments] == [30, 30, 30]
        # Contiguous by construction, which is what "段间无洞" means here.
        for previous, following in zip(plan.segments, plan.segments[1:]):
            assert following.first_frame == previous.first_frame + previous.cover
        assert (plan.segments[-1].first_frame + plan.segments[-1].cover
                == plan.coverage)
        # Every file has the preroll and the tail the encoder asked for.
        assert plan.segments[0].file_base_frame == 0
        assert plan.segments[0].encode_frames == 30 + TAIL_FRAMES
        assert plan.segments[1].file_base_frame == 30 - PREROLL_FRAMES
        assert plan.segments[1].encode_frames == PREROLL_FRAMES + 30 + TAIL_FRAMES
        assert plan.index_of(0) == 0 and plan.index_of(29) == 0
        assert plan.index_of(30) == 1 and plan.index_of(59) == 1
        assert plan.index_of(89) == 2 and plan.index_of(10_000) == 2


def test_the_grid_is_the_same_for_a_longer_lesson(tall_source):
    """An edit changes how many segments are needed, never what a cell contains."""
    with scratch('segments-stable-') as folder:
        short = plan_for(tall_source, coverage_seconds=2.0, cache_dir=folder)
        long = plan_for(tall_source, coverage_seconds=3.0, cache_dir=folder)
        assert len(short.segments) == 2 and len(long.segments) == 3
        for one, two in zip(short.segments, long.segments):
            assert one.first_frame == two.first_frame
            assert one.file_base_frame == two.file_base_frame
            assert one.encode_frames == two.encode_frames
            assert one.path == two.path, '同一格的文件名必须逐字相同'


def test_a_looping_source_never_puts_two_passes_in_one_segment():
    with scratch('segments-loop-') as folder:
        source = write_test_video(folder / 'short.mp4', seconds=2.0, fps=FPS,
                                  size=SOURCE_SIZE)
        # 2 s of source (60 frames) stretched over 5 s of lesson: two full passes.
        plan = plan_for(source, coverage_seconds=5.0, cache_dir=folder,
                        first_segment_seconds=1.0, segment_seconds=1.0)
        assert plan is not None and plan.source_total == 60
        assert [(segment.pass_index, segment.first_frame, segment.source_frame)
                for segment in plan.segments] == [
            (0, 0, 0), (0, 30, 29), (1, 60, 0), (1, 90, 29), (2, 120, 0)]
        for segment in plan.segments:
            # A pass boundary never carries a preroll from the previous pass.
            end = segment.source_frame + segment.encode_frames
            assert end <= plan.source_total, segment


def test_sources_segmentation_does_not_apply_to(tall_source):
    with scratch('segments-na-') as folder:
        # A picture with speed, a slice that does not start at zero, and a lesson that
        # one short segment would cover: all keep the single-file proxy.
        assert plan_for(tall_source, cache_dir=folder, speed=1.25) is None
        assert plan_for(tall_source, cache_dir=folder, source_start=1000) is None
        assert plan_for(tall_source, cache_dir=folder,
                        coverage_seconds=0.5) is None
        tiny = write_test_video(folder / 'tiny.mp4', seconds=1.0, fps=FPS, size='320x180')
        assert plan_for(tiny, cache_dir=folder) is None


def test_a_segment_holds_the_frames_it_promised(tall_source):
    with scratch('segments-encode-') as folder:
        plan = plan_for(tall_source, cache_dir=folder)
        segment = plan.ensure(1)
        assert segment.path.is_file() and plan.ready(1)
        measured = source_frames(segment.path, FPS, 1)
        assert measured >= segment.encode_frames - 1, (measured, segment.encode_frames)
        assert plan.ready_count() == 1
        # Encoding again is a lookup, not a second encode.
        before = segment.path.stat().st_mtime_ns
        plan.ensure(1)
        assert segment.path.stat().st_mtime_ns == before


def test_the_frame_at_a_segment_boundary_is_the_right_source_frame(tall_source):
    """The failure this guards is silent: frame 30 shown as frame 29, or missing.

    The reference is a **whole-file proxy** of the same source: same scale, same
    encoder settings, so the question "which source frame is this?" is answered by
    nearest-neighbour over a few candidates rather than by demanding two lossy encodes
    be byte-equal (they are not: a frame inside a GOP and a frame at a GOP start are
    quantised differently).
    """
    from word_video.media.proxy import proxy_video
    with scratch('segments-boundary-') as folder:
        proxy = folder / 'whole-proxy.mp4'
        proxy_video(tall_source, proxy, height=540)
        plan = plan_for(tall_source, cache_dir=folder)
        second = plan.ensure(1)
        # Segment 1 must contain item frame 30 as its own frame 1 (preroll of one).
        assert second.file_base_frame == 29
        signature = frame_signature(second.path, 0.5 / FPS)
        distances = {frame: signature_distance(signature,
                                               frame_signature(proxy, (frame + 0.5) / FPS))
                     for frame in range(27, 32)}
        nearest = min(distances, key=distances.get)
        assert nearest == second.file_base_frame == 29, distances
        ordered = sorted(distances.values())
        assert ordered[1] > ordered[0] * 2, distances   # 判定必须有区分力
        # And the frame that must be there *next* (item frame 30) is the next one.
        following = {frame: signature_distance(frame_signature(second.path, 1.5 / FPS),
                                               frame_signature(proxy, (frame + 0.5) / FPS))
                     for frame in range(27, 32)}
        assert min(following, key=following.get) == 30, following


def test_the_preparer_encodes_ahead_and_stops(tall_source):
    with scratch('segments-prepare-') as folder:
        plan = plan_for(tall_source, cache_dir=folder)
        preparer = SegmentPreparer(plan, ahead=1)
        preparer.start()
        try:
            preparer.focus(0)
            deadline = 30.0
            import time
            end = time.monotonic() + deadline
            while time.monotonic() < end and len(preparer.stats()['ready']) < 2:
                time.sleep(0.05)
            stats = preparer.stats()
            assert stats['ready'][:2] == [0, 1], stats
            assert stats['seconds'] > 0 and stats['failed'] == {}
        finally:
            assert preparer.stop(timeout=10.0) is True
        assert preparer.stats()['running'] is False


def session_for(source, cache_dir, *, coverage_seconds=COVERAGE_SECONDS,
                canvas_height=CANVAS):
    """A real session whose picture is the tall source, ready to be seeked.

    The segment grid is shortened to one second per cell: the product's own 6 s/15 s
    grid would need a lesson minutes long to have more than one cell, and the thing
    under test here is the wiring, not the length of a cell.
    """
    assets = three_tone_assets(cache_dir)
    tones = sorted(assets)
    video = background_item('layer:background', coverage_seconds)
    plan = plan_of([], video=(video,), total_seconds=coverage_seconds, fps=FPS,
                   width=1920, height=1080)
    paths = dict(assets)
    paths['layer:background'] = str(source)
    session = PreviewSession(plan, paths, temp_root=cache_dir / '.preview',
                             canvas_height=canvas_height, segment_seconds=1.0,
                             first_segment_seconds=1.0, prefetch_ahead=1)
    session.open()
    return session, tones


def test_a_seek_into_an_unprepared_segment_is_visible_and_then_appears(tall_source):
    with scratch('segments-session-') as folder:
        session, _ = session_for(tall_source, folder)
        try:
            assert session.segment_plan is not None
            plan = session.segment_plan
            # open() materialises the first segment and starts the background thread;
            # stop it here so "not prepared yet" is a fact rather than a race, and
            # aim at the *last* segment, which the thread has no time to reach.
            assert session.preparer.stop(timeout=10.0) is True
            target = plan.segments[-1]
            assert plan.ready(0) is True and plan.ready(target.index) is False

            session.seek(target.first_frame * session._item_frame_ticks()
                         + 5 * session._item_frame_ticks())
            waiting = session.snapshot()
            assert waiting.preparing, '没准备好的区间必须说出来'
            assert waiting.frame is None
            assert '正在准备' in waiting.preparing
            assert waiting.to_dict()['preparing'] == waiting.preparing

            # Once the segment exists, the next paint picks it up - no new seek.
            plan.ensure(target.index)
            appeared = None
            import time
            deadline = time.monotonic() + 20.0
            while time.monotonic() < deadline:
                presentation = session.snapshot()
                if presentation.frame is not None:
                    appeared = presentation
                    break
                time.sleep(0.02)
            assert appeared is not None, '段准备好之后画面必须出现'
            assert appeared.preparing == ''
            assert appeared.segment == target.index
            # The picture shown belongs to the segment's own span of the timeline.
            frame_ticks = session._item_frame_ticks()
            item_frame = appeared.frame.pts_ticks // frame_ticks
            assert target.first_frame <= item_frame <= target.last_frame + 1
            assert 0 <= appeared.frame.index < target.cover + PREROLL_FRAMES + 1
        finally:
            report = session.close()
        assert report['preparer_stopped'] is True
        assert report['children_left'] == 0 and report['threads_left'] == 0


def test_the_decoder_is_pointed_at_the_segment_that_holds_the_position(tall_source):
    with scratch('segments-spec-') as folder:
        session, _ = session_for(tall_source, folder)
        try:
            session.preparer.stop(timeout=10.0)
            plan = session.segment_plan
            plan.ensure(1)
            ticks = int(1.5 * TICKS_PER_SECOND)
            session._use_segment(1, session.clock.generation, ticks)
            spec = session.decode_spec
            segment = plan.segment(1)
            assert Path(spec.path) == segment.path
            # A segment is not a loop period: the item frame index stays global.
            assert spec.source_frames == 0 and spec.loop is False
            # The decoder uses one index both to seek the file and to stamp what it
            # reads, so the spec describes *this file*: frame 0 of the segment is item
            # frame ``file_base_frame``, and that is the tick the stamps start from.
            frame_ticks = session._item_frame_ticks()
            assert spec.start_ticks == segment.file_base_frame * frame_ticks
            assert spec.offset_frames == int(1.5 * FPS) - segment.file_base_frame
            assert (spec.start_ticks + spec.offset_frames * frame_ticks
                    == int(1.5 * FPS) * frame_ticks)
            # ... and "which item frame is this position on" did not move with it.
            assert session._item_frame_at(ticks) == int(1.5 * FPS)
            assert session.segment_index == 1 and session.preparing == ''
        finally:
            session.close()


def test_a_source_without_segmentation_still_uses_one_proxy_file(tall_source):
    """The old path is untouched where segmentation does not apply."""
    with scratch('segments-fallback-') as folder:
        assets = three_tone_assets(folder)
        video = background_item('layer:background', 0.5)
        plan = plan_of([], video=(video,), total_seconds=0.5, fps=FPS,
                       width=1920, height=1080)
        paths = dict(assets)
        paths['layer:background'] = str(tall_source)
        session = PreviewSession(plan, paths, temp_root=folder / '.preview',
                                 canvas_height=CANVAS)
        session.open()
        try:
            assert session.segment_plan is None
            entry = session.sources['layer:background']
            assert entry.is_proxy is True and entry.path.endswith('.mp4')
            assert '.seg' not in Path(entry.path).name
        finally:
            report = session.close()
        assert report['segments'] is None
        assert report['preparer'] is None


# -- 首帧成本：不探测不该探测的东西，重复问同一个问题不重算 ------------------
def test_opening_a_session_probes_the_picture_and_nothing_else(tall_source):
    """Speech assets are recorded as-is: the decoder never opens them.

    Measured 2026-09-16: asking ffprobe for the height of all 150 speech files cost
    24 s of *every* session open, including the rebuild after each committed edit, and
    the answer ("not a picture") was thrown away by the only caller that could use it.
    """
    from preview import segments as segments_module
    from preview import sources as sources_module

    calls = []
    real = sources_module.video_stream

    def counting(path):
        calls.append(str(path))
        return real(path)

    sources_module.video_stream = counting
    segments_module.video_stream = counting
    try:
        with scratch('preview-no-probe-') as folder:
            session, _ = session_for(tall_source, folder)
            try:
                assert sorted(session.sources) == sorted(session.assets)
                # Only the picture was asked about - and then only about its height.
                assert [Path(call).name for call in calls
                        if 'tone' in Path(call).name] == []
                assert any(Path(call) == tall_source for call in calls)
            finally:
                session.close()
    finally:
        sources_module.video_stream = real
        segments_module.video_stream = real


def test_the_same_question_about_one_file_is_answered_once(tall_source):
    """A rebuilt session must not re-probe a source whose answer cannot have changed."""
    from preview import sources as sources_module
    from preview.sources import clear_resolution_cache, resolve_preview_source

    calls = []
    real = sources_module.video_stream

    def counting(path):
        calls.append(str(path))
        return real(path)

    sources_module.video_stream = counting
    try:
        with scratch('preview-memo-') as folder:
            first = resolve_preview_source(tall_source, 360, cache_dir=folder)
            second = resolve_preview_source(tall_source, 360, cache_dir=folder)
            assert first == second and len(calls) == 1
            # A *pruned* proxy cannot be handed out from memory: the answer is remade.
            Path(first.path).unlink()
            third = resolve_preview_source(tall_source, 360, cache_dir=folder)
            assert third.is_proxy is True and Path(third.path).is_file()
            assert len(calls) >= 2
    finally:
        sources_module.video_stream = real
        clear_resolution_cache()


def test_playing_across_a_segment_boundary_keeps_the_picture_moving(tall_source):
    """Crossing a boundary is a decoder restart, not a seek: the picture keeps coming.

    The clock is moved by hand (``ManualAudioOutput``), so this runs in milliseconds
    and gives the same answer every time.  What it asserts is what can be asserted from
    a hand-driven clock: the picture advances past the boundary, its timestamps never go
    backwards or run ahead of the clock, the session never falls into "preparing" while
    playing (the prefetcher stayed ahead of the playhead), and the decoder reports no
    stall or failure.  *Which* source frame sits at the join is checked exactly, frame
    by frame, by ``test_the_frame_at_a_segment_boundary_is_the_right_source_frame``.
    """
    from preview.clock import ManualAudioOutput

    with scratch('segments-play-') as folder:
        output = ManualAudioOutput(rate=RATE)
        assets = three_tone_assets(folder)
        video = background_item('layer:background', COVERAGE_SECONDS)
        plan = plan_of([], video=(video,), total_seconds=COVERAGE_SECONDS, fps=FPS,
                       width=1920, height=1080)
        paths = dict(assets)
        paths['layer:background'] = str(tall_source)
        session = PreviewSession(plan, paths, output=output,
                                 temp_root=folder / '.preview', canvas_height=CANVAS,
                                 segment_seconds=1.0, first_segment_seconds=1.0,
                                 prefetch_ahead=1)
        session.open()
        try:
            assert session.segment_plan is not None
            plan = session.segment_plan
            # Every segment is made ready *before* playback starts, and the background
            # thread is stopped so nothing competes with the clock.  The subject here is
            # the boundary switch, not the prefetcher (which has its own tests): on a
            # loaded machine a prefetcher that is one segment behind would make this
            # test say "the picture stalled" about something that is not the switch.
            assert session.preparer.stop(timeout=10.0) is True
            for index in range(len(plan.segments)):
                plan.ensure(index)
            session.play(0)
            delivered = []
            step = int(0.02 * RATE)
            deadline = time.monotonic() + 120.0
            # Real-time pacing: 20 ms of clock per 20 ms of wall time.
            while len(delivered) < 2 * FPS and time.monotonic() < deadline:
                output.advance(step)
                presentation = session.snapshot()
                frame = presentation.frame
                # ``snapshot`` returns the picture *due now*, which stays the same frame
                # until a newer one arrives; the sequence under test is the changes.
                if frame is not None and (not delivered
                                          or delivered[-1].pts_ticks != frame.pts_ticks):
                    delivered.append(frame)
                assert presentation.preparing == '', '播放中不该停在"准备中"：%r' % presentation.preparing
                time.sleep(0.02)
            assert len(delivered) >= 2 * FPS, '播放跨段时画面停了：只拿到 %d 帧' % len(delivered)
            stamps = [frame.pts_ticks for frame in delivered]
            assert stamps == sorted(stamps), stamps[:10]      # 换段不会回放更早的时刻
            # And the picture never ran ahead of the clock it is drawn against.
            assert stamps[-1] <= session.position_ticks() + 2 * (TICKS_PER_SECOND // FPS)
            # Frames arrived from more than one segment: the switch really happened.
            groups = {session.segment_plan.index_of(stamp // (TICKS_PER_SECOND // FPS))
                      for stamp in stamps}
            assert len(groups) >= 2 and 0 in groups, groups
            assert session.segment_index >= 1
            decode = session.report()['decode']
            assert decode['failures'] == 0 and decode['stalls'] == 0, decode
        finally:
            report = session.close()
        assert report['preparer_stopped'] is True
        assert report['children_left'] == 0 and report['threads_left'] == 0


def test_the_preview_audio_cache_is_prepared_for_every_speech_asset(area=None):
    """Parallel preparation produces exactly the same set of files, in one pass."""
    from desktop.editor_project import default_prepare_workers
    from test_editor_support import tiny_state

    assert default_prepare_workers(1) == 1 and default_prepare_workers(150) <= 8
    with scratch('preview-audio-') as folder:
        project_folder, _ = tiny_state(folder / 'project')
        first = project_folder.preview_assets(project_folder.project)
        assert sorted(first) == ['w1:chinese', 'w1:female', 'w1:male']
        assert all(Path(path).is_file() for path in first.values())
        # Second call is a lookup: same paths, nothing rewritten.
        stamps = {key: Path(path).stat().st_mtime_ns for key, path in first.items()}
        second = project_folder.preview_assets(project_folder.project)
        assert second == first
        assert {key: Path(path).stat().st_mtime_ns for key, path in second.items()} == stamps
