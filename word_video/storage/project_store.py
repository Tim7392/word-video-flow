"""Project storage: ``project.json`` is the one authoritative document.

Saving is atomic on the same volume — a unique temporary file beside the target,
then :func:`os.replace` — so a crash can never leave a half-written project and a
reader either sees the previous revision or the new one (architecture §10).
Loading is strict: an unknown document version, an unknown field or a missing
field is refused instead of being opened and later saved over.
"""
from pathlib import Path
import json
import os
import uuid

from ..domain.errors import SchemaError
from ..domain.model import Project

PROJECT_FILENAME = 'project.json'


def project_path(path):
    """Accept either the project directory or the document path itself."""
    target = Path(path)
    return target / PROJECT_FILENAME if target.is_dir() else target


def write_document(document, target):
    """Write one JSON document atomically and return the path it was written to.

    The one place the staging rule lives: a unique temporary file beside the
    target, fsync, then :func:`os.replace`, so a crash leaves either the previous
    revision or the new one.  ``allow_nan=False`` keeps a non-finite number out of
    every document this package writes.
    """
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(document, ensure_ascii=False, sort_keys=True,
                      indent=2, allow_nan=False) + '\n'
    temporary = target.with_name('.%s.%s.tmp' % (target.name, uuid.uuid4().hex))
    try:
        with open(temporary, 'w', encoding='utf-8', newline='\n') as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
    return target


def read_document(source, name):
    """Read one JSON object document, refusing anything unusable."""
    source = Path(source)
    try:
        text = source.read_text(encoding='utf-8')
    except FileNotFoundError:
        raise SchemaError('no %s at %s' % (name, source), path=name) from None
    try:
        value = json.loads(text)
    except ValueError as error:
        raise SchemaError('%s is not valid JSON: %s' % (name, error),
                          path=name) from None
    if not isinstance(value, dict):
        raise SchemaError('%s must be a JSON object' % name, path=name)
    return value


def save_project(project, path):
    """Write one project revision atomically and return the document path."""
    if not isinstance(project, Project):
        raise SchemaError('save needs a Project document', path='project')
    return write_document(project.to_dict(), project_path(path))


def load_project(path):
    """Read and validate one project document."""
    return Project.from_dict(read_document(project_path(path), 'project'))
