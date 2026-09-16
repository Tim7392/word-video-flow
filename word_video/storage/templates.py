"""Template storage: one file per (template, version), plus the built-in reference.

Layout: ``<root>/templates/<template_id>/v<version>.json``.  A version is never
rewritten in place — a new one gets the next number — so a batch's "template X@2"
stays true after X@3 exists, and an upgrade is undone by pointing back at 2.

The reference template (``lesson-default@1``, the layout the verified engine draws)
is always loadable and needs no file: a fresh root has one template, not zero.  A
stored file with that id takes over, so a member who edits the reference gets their
own versions instead of a silent merge with the built-in.

A template id is a folder name, so it is checked against a conservative pattern: a
document that could climb out of the templates folder is refused rather than
resolved.
"""
from pathlib import Path
import re

from ..application.templates import SCHEMA, TemplateDocument
from ..domain.errors import SchemaError
from ..domain.lesson import DEFAULT_LESSON_TEMPLATE
from .project_store import read_document, write_document

TEMPLATES_DIR = 'templates'
BUILTIN_TEMPLATE_ID = DEFAULT_LESSON_TEMPLATE.template_id
_ID = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]*$')
_VERSION_FILE = re.compile(r'^v(\d+)\.json$')

BUILTIN_TEMPLATE = TemplateDocument(template_id=BUILTIN_TEMPLATE_ID, version=1,
                                    lesson=DEFAULT_LESSON_TEMPLATE,
                                    note='内置参考模板：与已验证引擎同一版式')


def checked_id(template_id):
    """A template id that is safe to use as one folder name, or a refusal."""
    if not isinstance(template_id, str) or not _ID.match(template_id) \
            or template_id in ('.', '..'):
        raise SchemaError('template id %r is not usable as a folder name'
                          % (template_id,), path='template',
                          hint='模板 id 只用字母、数字、点、下划线和连字符')
    return template_id


class TemplateStore:
    """The templates under one work root; reading never writes."""

    def __init__(self, root):
        self.root = Path(root).resolve()
        self.folder = self.root / TEMPLATES_DIR

    # -- layout ----------------------------------------------------------
    def template_folder(self, template_id):
        return self.folder / checked_id(template_id)

    def path(self, template_id, version):
        version = _checked_version(version)
        return self.template_folder(template_id) / ('v%d.json' % version)

    def versions(self, template_id):
        """Every stored version of one template, oldest first."""
        folder = self.template_folder(template_id)
        if not folder.is_dir():
            return []
        found = []
        for item in sorted(folder.iterdir()):
            match = _VERSION_FILE.match(item.name)
            if match and item.is_file():
                found.append(int(match.group(1)))
        return sorted(found)

    def template_ids(self):
        """Every id a caller can ask for: stored ones plus the built-in."""
        stored = sorted(item.name for item in self.folder.iterdir()
                        if item.is_dir()) if self.folder.is_dir() else []
        known = list(stored)
        if BUILTIN_TEMPLATE_ID not in known:
            known.append(BUILTIN_TEMPLATE_ID)
        return known

    def latest(self, template_id):
        versions = self.versions(template_id)
        if versions:
            return versions[-1]
        return BUILTIN_TEMPLATE.version if template_id == BUILTIN_TEMPLATE_ID else None

    # -- reading ---------------------------------------------------------
    def load(self, template_id, version=None):
        """One template version; the built-in answers when nothing is stored."""
        checked_id(template_id)
        versions = self.versions(template_id)
        if version is None:
            if not versions:
                return self._builtin(template_id)
            version = versions[-1]
        version = _checked_version(version)
        path = self.path(template_id, version)
        if not path.is_file():
            if template_id == BUILTIN_TEMPLATE_ID and version == BUILTIN_TEMPLATE.version:
                return BUILTIN_TEMPLATE
            raise SchemaError('no template %s@%d' % (template_id, version),
                              path='template',
                              hint='已存版本：%s' % (versions or ['（无）']))
        document = TemplateDocument.from_dict(read_document(path, 'template'))
        if document.template_id != template_id or document.version != version:
            raise SchemaError(
                'template file %s says %s@%d; the folder it lives in says %s@%d'
                % (path, document.template_id, document.version, template_id, version),
                path='template', hint='文件名与文档内容不一致，拒绝按两个不同的身份读取')
        return document

    def _builtin(self, template_id):
        if template_id != BUILTIN_TEMPLATE_ID:
            raise SchemaError('no template %r' % (template_id,), path='template',
                              hint='可用模板：%s' % '、'.join(self.template_ids()))
        return BUILTIN_TEMPLATE

    def source(self, template_id, version=None):
        """``'builtin'``, ``'stored'`` or ``'missing'``: where a load would come from."""
        versions = self.versions(template_id)
        if versions:
            if version is not None and _checked_version(version) not in versions:
                return 'missing'
            return 'stored'
        if template_id == BUILTIN_TEMPLATE_ID \
                and (version is None
                     or _checked_version(version) == BUILTIN_TEMPLATE.version):
            return 'builtin'
        return 'missing'

    def index(self):
        """What exists, for ``template list``: ids, versions and unreadable files."""
        templates, problems = [], []
        for template_id in self.template_ids():
            versions = self.versions(template_id)
            entry = {'template_id': template_id, 'versions': versions,
                     'latest': self.latest(template_id),
                     'builtin': not versions and template_id == BUILTIN_TEMPLATE_ID}
            if versions:
                entry['path'] = str(self.template_folder(template_id))
                try:
                    document = self.load(template_id, versions[-1])
                except SchemaError as error:
                    problems.append({'template_id': template_id,
                                     'path': str(self.path(template_id, versions[-1])),
                                     'error': str(error)})
                else:
                    entry['intro'] = document.intro
                    entry['styles'] = sorted(document.style_table())
                    entry['note'] = document.note
            else:
                entry['intro'] = BUILTIN_TEMPLATE.intro
                entry['styles'] = []
                entry['note'] = BUILTIN_TEMPLATE.note
            templates.append(entry)
        for item in sorted(self.folder.iterdir()) if self.folder.is_dir() else []:
            if item.is_file() and item.suffix == '.json':
                problems.append({'template_id': '', 'path': str(item),
                                 'error': '模板要放在 <root>/%s/<id>/v<N>.json'
                                          % TEMPLATES_DIR})
        return {'templates': templates, 'problems': problems}

    # -- writing ---------------------------------------------------------
    def save(self, document):
        """Write one version next to the others; returns the file it wrote.

        Only a *new* version number is written: asking to save over an existing
        version is refused, because that version may already be what a batch was
        planned against.
        """
        if not isinstance(document, TemplateDocument):
            raise SchemaError('save needs a TemplateDocument', path='template')
        path = self.path(document.template_id, document.version)
        if path.exists():
            raise SchemaError('template %s@%d already exists'
                              % (document.template_id, document.version),
                              path='template',
                              hint='升级要写下一个版本号，已存在的版本不被覆盖')
        return write_document(document.to_dict(), path)


def _checked_version(version):
    if isinstance(version, bool) or not isinstance(version, int) or version <= 0:
        raise SchemaError('template version must be a positive integer',
                          path='template')
    return version


__all__ = ['BUILTIN_TEMPLATE', 'BUILTIN_TEMPLATE_ID', 'SCHEMA', 'TEMPLATES_DIR',
           'TemplateStore', 'checked_id']
