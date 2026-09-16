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


def save_project(project, path):
    """Write one project revision atomically and return the document path."""
    if not isinstance(project, Project):
        raise SchemaError('save needs a Project document', path='project')
    target = project_path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    # allow_nan=False: a non-finite number can never reach the document on disk.
    text = json.dumps(project.to_dict(), ensure_ascii=False, sort_keys=True,
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


def load_project(path):
    """Read and validate one project document."""
    source = project_path(path)
    try:
        text = source.read_text(encoding='utf-8')
    except FileNotFoundError:
        raise SchemaError('no project document at %s' % source, path='project') from None
    try:
        value = json.loads(text)
    except ValueError as error:
        raise SchemaError('project document is not valid JSON: %s' % error,
                          path='project') from None
    if not isinstance(value, dict):
        raise SchemaError('project document must be a JSON object', path='project')
    return Project.from_dict(value)
