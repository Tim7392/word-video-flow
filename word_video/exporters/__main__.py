"""One command for the three deliverables: ``python -m word_video.exporters``.

``word_video_cli.py`` belongs to A and its contract is covered by a black-box
test, so this entry point is deliberately separate: it does not change that
parser's registered actions, and A can wire the same
:func:`word_video.exporters.render.export_run` underneath a CLI subcommand when
the contract version is raised.

    python -m word_video.exporters --project <dir|project.json> [--out DIR]

**stdout carries exactly one JSON object for every outcome** - a published run, a
refused project, bad arguments, or a crash - and the exit code says which:

* ``0`` the deliverables were published: ``{"ok": true, "result": {...}}``;
* ``2`` the caller has to supply or repair something: a bad flag
  (``BAD_REQUEST``), or a refusal that names what is wrong with the input.  That
  is A's ``ProjectError`` family (``ASSET_FILE_MISSING``, ``MISSING_ROLE``,
  ``TEACHING_ORDER``, ...) and this package's own refusals
  (``ExportError``/``CatalogError``/``ProjectionError``/``BackgroundError``), plus
  the ``ValueError``/``OSError``/``RuntimeError`` this entry point has always
  answered as refusals - a bad value, a file that is not there, a tool that failed;
* ``1`` anything else: reported as ``INTERNAL``, with the traceback on **stderr**
  and the same one-JSON object on stdout.

The error document has A's CLI shape, which is what a headless caller parses::

    {"ok": false, "error": {"type", "code", "message", "object_path", "hint",
                            "fixes"}}

``fixes`` is always present and is empty when the error names no separate list -
for a project refusal the ``hint`` is the way forward, as it is in
``python -m word_video.cli``.  W10's failure-isolation gate found this entry
leaking a ``ProjectError`` traceback with an empty stdout, which broke that
contract for the headless path; the editor wraps the same error into a notice, so
members never saw the gap.
"""
import argparse
import json
import sys
import traceback

from ..domain.errors import ProjectError


class BadRequest(ProjectError):
    """Bad usage or arguments: still one JSON object, still exit code 2."""

    code = 'BAD_REQUEST'


class _Parser(argparse.ArgumentParser):
    """Argparse's own errors must leave one JSON object on stdout, not a usage line."""

    def error(self, message):
        raise BadRequest(message, path='cli',
                         hint='python -m word_video.exporters --help 列出可用参数')


def build_parser():
    parser = _Parser(
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


def own_refusals():
    """This package's refusals, as ``(class, stable code)``.

    They carry no ``code`` of their own - A's family does - so the entry point is
    where one has to be fixed, and it is fixed here once for both the headless CLI
    and any caller that reads the same document.
    """
    from .background import BackgroundError
    from .catalog import CatalogError
    from .plan import ProjectionError
    from .render import ExportError

    return ((ExportError, 'EXPORT_REFUSED'), (CatalogError, 'ASSET_CATALOG'),
            (ProjectionError, 'PROJECTION'), (BackgroundError, 'BACKGROUND'))


def refusal_types():
    """The exceptions that mean "the caller has to fix the input" (exit 2)."""
    return (ProjectError, ValueError, OSError, RuntimeError) + tuple(
        family for family, _ in own_refusals())


def error_document(error):
    """One failure in A's CLI shape, whatever kind of exception it is."""
    code = str(getattr(error, 'code', '') or '')
    if not code:
        for family, name in own_refusals():
            if isinstance(error, family):
                code = name
                break
    return {'type': type(error).__name__, 'code': code or 'REFUSED',
            'message': str(error),
            'object_path': str(getattr(error, 'path', '') or ''),
            'hint': str(getattr(error, 'hint', '') or ''),
            'fixes': [str(item) for item in (getattr(error, 'fixes', ()) or ())]}


def write_json(value, stream=None):
    """One JSON object, one line, on the stream that carries the answer."""
    stream = stream or sys.stdout
    stream.write(json.dumps(value, ensure_ascii=False) + '\n')
    stream.flush()


def fail(error, code):
    """Report a refusal: the code and message on stderr, the document on stdout."""
    document = error_document(error)
    print('%s: %s' % (document['code'], error), file=sys.stderr)
    write_json({'ok': False, 'error': document})
    return code


def crash(error):
    """Report an unexpected failure: the traceback on stderr, one JSON on stdout."""
    traceback.print_exc(file=sys.stderr)
    write_json({'ok': False, 'error': {
        'type': type(error).__name__, 'code': 'INTERNAL',
        'message': str(error)[:1000], 'object_path': '', 'hint': '', 'fixes': []}})
    return 1


def main(argv=None):
    try:
        args = build_parser().parse_args(argv)
    except BadRequest as error:
        return fail(error, 2)
    from .render import export_run
    try:
        result = export_run(args.project, output=args.out, run_name=args.run,
                            background=args.background,
                            intro_video=args.intro_video,
                            intro_audio=args.intro_audio, video_codec=args.codec,
                            slices=args.slices, render=not args.no_video,
                            draft=not args.no_draft)
    except refusal_types() as error:
        return fail(error, 2)
    except Exception as error:                     # noqa: BLE001 - one JSON, always
        return crash(error)
    write_json({'ok': True, 'result': result.to_dict()})
    return 0


if __name__ == '__main__':
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    if hasattr(sys.stderr, 'reconfigure'):
        sys.stderr.reconfigure(encoding='utf-8', errors='replace')
    sys.exit(main())


__all__ = ['BadRequest', 'build_parser', 'error_document', 'main',
           'own_refusals', 'refusal_types', 'write_json']
