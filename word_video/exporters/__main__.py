"""One command for the three deliverables: ``python -m word_video.exporters``.

``word_video_cli.py`` belongs to A and its contract is covered by a black-box
test, so this entry point is deliberately separate: it does not change that
parser's registered actions, and A can wire the same
:func:`word_video.exporters.render.export_run` underneath a CLI subcommand when
the contract version is raised.

    python -m word_video.exporters --project <dir|project.json> [--out DIR]

stdout carries one JSON object (the same discipline as the existing CLI):
``{"ok": true, "result": {...}}`` on success,
``{"ok": false, "error": {...}}`` with exit code 2 on a refusal.
"""
import argparse
import json
from pathlib import Path
import sys


def build_parser():
    parser = argparse.ArgumentParser(
        prog='python -m word_video.exporters',
        description='Export MP4 + five SRT + editable draft from one project.')
    parser.add_argument('--project', required=True,
                        help='project directory or project.json')
    parser.add_argument('--out', default=None, help='output directory')
    parser.add_argument('--run', default='', help='run folder name inside --out')
    parser.add_argument('--background', default=None,
                        help='background media for this delivery')
    parser.add_argument('--intro-video', default='',
                        help='reference countdown clip (its own sound wins)')
    parser.add_argument('--intro-audio', default='',
                        help='intro sound when the clip has none')
    parser.add_argument('--codec', default='h264', choices=('h264', 'h265'))
    parser.add_argument('--slices', type=int, default=None,
                        help='render slices; 1 forces one ffmpeg process')
    parser.add_argument('--no-video', action='store_true',
                        help='produce SRT and draft only')
    parser.add_argument('--no-draft', action='store_true',
                        help='skip the editable draft')
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    from word_video.exporters.render import export_run
    try:
        result = export_run(args.project, output=args.out, run_name=args.run,
                            background=args.background,
                            intro_video=args.intro_video,
                            intro_audio=args.intro_audio, video_codec=args.codec,
                            slices=args.slices, render=not args.no_video,
                            draft=not args.no_draft)
    except (ValueError, OSError, RuntimeError) as error:
        print(json.dumps({'ok': False,
                          'error': {'type': type(error).__name__,
                                    'message': str(error)}}, ensure_ascii=False))
        return 2
    print(json.dumps({'ok': True, 'result': result.to_dict()}, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    sys.exit(main())
