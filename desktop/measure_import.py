"""先量后改：一次真实导入与首次预览的秒数，按阶段拆开（H0 2026-09-16 派工）。

The member-visible complaint is "50 words take six minutes".  That sentence is not
actionable; "probing 150 assets is 103 s of a 107 s import, and the first preview
spends 60 s encoding a proxy of the whole lesson before showing one frame" is.  So
this script drives the *same* functions the editor drives - ``import_request`` with
its phase recorder, ``ProjectFolder.preview_assets``, ``PreviewSession`` - and prints
where the time went, before and after a change.

It is a measurement entry point, not a second implementation: every step calls the
production function, and the phase numbers come from inside ``import_request``.

    python -m desktop.measure_import --request <请求.json> --into <工程目录> [--report out.json]

``--seek-seconds`` additionally measures a jump to a position the preview may not have
prepared yet, which is the behaviour the H0 note demands be explicit rather than a
black frame.
"""
import argparse
import json
import sys
import time
from pathlib import Path

from .editor_import import import_request
from .editor_model import EditorState
from .editor_project import with_background

CANVAS_HEIGHT = 720


def directory_facts(folder, *names):
    """File count and bytes of the cache directories this run wrote."""
    found = {}
    for name in names:
        target = folder / name
        files = [path for path in target.rglob('*') if path.is_file()] if target.is_dir() else []
        found[name] = {'files': len(files), 'bytes': sum(path.stat().st_size for path in files)}
    return found


def open_session(folder, state, *, canvas_height=CANVAS_HEIGHT, first_frame_timeout=90.0,
                 segments=True, segment_seconds=None, first_segment_seconds=None):
    """One preview session over the current plan, exactly as the window opens it.

    The session is asked for the frame at 0 after opening, because that is what the
    member sees: opening alone resolves the sources, and nothing decodes until a
    position is requested (the window does it on 播放 or on the first seek).
    """
    from preview.session import (DEFAULT_SEGMENT_SECONDS, FIRST_SEGMENT_SECONDS,
                                 PreviewSession, canvas_size_for)

    plan = state.plan()
    if plan is None:
        return None, {'plan_error': str(state.plan_error or '')}
    background = folder.background_slice(state.project)
    preview_plan = with_background(plan, state.project, background)
    assets = folder.preview_assets(state.project)
    if background is not None:
        assets['layer:background'] = folder.delivery.background
    if not segments:
        # The single-file proxy: one segment long enough to be the whole lesson, which
        # is exactly what the preview did before segmentation existed.
        segment_seconds = first_segment_seconds = 10 ** 6
    session = PreviewSession(
        preview_plan, assets, temp_root=folder.preview_dir, canvas_height=canvas_height,
        segment_seconds=segment_seconds or DEFAULT_SEGMENT_SECONDS,
        first_segment_seconds=first_segment_seconds or FIRST_SEGMENT_SECONDS)
    started = time.monotonic()
    session.open()
    opened = time.monotonic() - started
    started = time.monotonic()
    session.seek(0)
    first = session.wait_for_frame(timeout=first_frame_timeout)
    return session, {'open_seconds': round(opened, 3),
                     'first_frame_seconds': None if first is None else round(first, 3),
                     'first_frame_wait_seconds': round(time.monotonic() - started, 3),
                     'first_frame_timeout': first is None,
                     'canvas': '%dx%d' % (session.canvas_width, session.canvas_height),
                     'assets': len(assets),
                     'segment_error': session.segment_error}


def open_folder_state(path):
    """The window's own state for a folder: media table *and* the intro measurement.

    Both, because a plan solved without the intro measurement is not the plan the
    window solves - the first version of this script measured a plan the editor never
    builds, and reported a preview that could not exist.
    """
    from .editor_project import ProjectFolder

    folder = ProjectFolder.open(path)
    state = EditorState(folder.project, media=folder.media_table(),
                        path=folder.project_path)
    state.set_media_table(folder.media_table())
    state.set_intro(folder.intro_measurement())
    return folder, state


def measure(request_path, into, *, seek_seconds=None, canvas_height=CANVAS_HEIGHT,
            workers=None, segments=True, segment_seconds=None, first_segment_seconds=None):
    timings = []
    started = time.monotonic()
    folder = import_request(request_path, into, timings=timings, workers=workers)
    total = time.monotonic() - started
    report = {'request': str(request_path), 'into': str(into),
              'import': {'seconds': round(total, 3), 'phases': timings,
                         'phases_total': round(sum(row['seconds'] for row in timings), 3),
                         'records': len(folder.project.records),
                         'clips': len(folder.project.clips),
                         'assets': len(folder.refs)}}
    return measure_preview(folder, report, seek_seconds=seek_seconds,
                           canvas_height=canvas_height, segments=segments,
                           segment_seconds=segment_seconds,
                           first_segment_seconds=first_segment_seconds)


def measure_preview(folder, report, *, seek_seconds=None, canvas_height=CANVAS_HEIGHT,
                    segments=True, segment_seconds=None, first_segment_seconds=None):
    state = EditorState(folder.project, media=folder.media_table(),
                        path=folder.project_path)
    state.set_media_table(folder.media_table())
    state.set_intro(folder.intro_measurement())
    plan = state.plan()
    report['plan'] = {'ok': plan is not None, 'error': str(state.plan_error or ''),
                      'seconds': None if plan is None else round(plan.total_ticks / 720000.0, 3),
                      'frames': None if plan is None else int(plan.total_ticks
                                                              // (720000 // plan.fps_num))}
    if plan is None:
        return report

    started = time.monotonic()
    prepared = folder.preview_assets(folder.project)
    report['preview_audio'] = {'seconds': round(time.monotonic() - started, 3),
                               'files': len(prepared)}
    report['cache_after_audio'] = directory_facts(folder.path, '.preview')
    modes = {'segments': segments, 'segment_seconds': segment_seconds,
             'first_segment_seconds': first_segment_seconds}

    session, opened = open_session(folder, state, canvas_height=canvas_height, **modes)
    report['first_preview'] = opened
    if session is not None and seek_seconds is not None:
        started = time.monotonic()
        session.seek(int(float(seek_seconds) * 720000))
        # The state *while waiting* is the evidence H0 asked for: a seek into a
        # segment nobody prepared must say so rather than show a black frame.
        immediate = session.snapshot()
        waited = session.wait_for_frame(timeout=90.0)
        snapshot = session.snapshot()
        report['seek_unprepared'] = {'to_seconds': float(seek_seconds),
                                     'seconds': round(time.monotonic() - started, 3),
                                     'preparing_while_waiting': immediate.preparing,
                                     'frame_while_waiting': immediate.frame is not None,
                                     'wait_for_frame': waited, 'state': snapshot.state,
                                     'preparing': snapshot.preparing,
                                     'segment': snapshot.segment,
                                     'frame_index': getattr(snapshot.frame, 'index', None)}
    if session is not None:
        report['first_preview']['session'] = session.report()
        session.close()

    session, opened = open_session(folder, state, canvas_height=canvas_height, **modes)
    report['second_preview'] = opened
    if session is not None:
        session.close()
    report['cache_after_preview'] = directory_facts(folder.path, '.preview')
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(prog='python -m desktop.measure_import',
                                     description='导入四段 + 首次预览的实测秒数')
    parser.add_argument('--request', default='', help='请求 JSON（与 --into 一起用）')
    parser.add_argument('--into', default='', help='工程目录（导入写入这里）')
    parser.add_argument('--open', dest='open_dir', default='',
                        help='只测预览：打开一个已存在的工程目录')
    parser.add_argument('--seek-seconds', type=float, default=None,
                        help='再量一次跳到这个秒数（可能是未生成区间）')
    parser.add_argument('--canvas-height', type=int, default=CANVAS_HEIGHT)
    parser.add_argument('--workers', type=int, default=None,
                        help='资产探测并发数；1 = 逐条串行（改动前的行为）')
    parser.add_argument('--no-segments', dest='segments', action='store_false',
                        help='关掉分段代理：整段一个代理文件（改动前的行为）')
    parser.add_argument('--segment-seconds', type=float, default=None,
                        help='分段长度（默认 15s；首段另有 --first-segment-seconds）')
    parser.add_argument('--first-segment-seconds', type=float, default=None,
                        help='首段长度（默认 6s，决定首帧要等多久）')
    parser.add_argument('--report', default='')
    args = parser.parse_args(argv)
    modes = {'segments': args.segments, 'segment_seconds': args.segment_seconds,
             'first_segment_seconds': args.first_segment_seconds}
    if args.open_dir:
        from .editor_project import ProjectFolder

        report = {'open': args.open_dir}
        folder = ProjectFolder.open(args.open_dir)
        folder.measure()
        report = measure_preview(folder, report, seek_seconds=args.seek_seconds,
                                 canvas_height=args.canvas_height, **modes)
    else:
        if not (args.request and args.into):
            parser.error('要么给 --request 与 --into，要么给 --open')
        report = measure(args.request, args.into, seek_seconds=args.seek_seconds,
                         canvas_height=args.canvas_height, workers=args.workers, **modes)
    text = json.dumps(report, ensure_ascii=False, indent=1)
    if args.report:
        Path(args.report).write_text(text, encoding='utf-8')
    # Written as bytes: a Windows console with a legacy code page must not be able to
    # turn a phase name into a crash after the measurement has already succeeded.
    sys.stdout.buffer.write((text + '\n').encode('utf-8'))
    sys.stdout.buffer.flush()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
