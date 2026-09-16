"""A template package: the versioned document in a folder a member can hand over.

A template is already shareable as one file, so why a package?  Because "shareable"
has to be *checkable*: the moment a document leaves one work root for another, it must
carry no absolute working path, no credential and no executable, and the receiving side
must be able to prove that before it reads anything into its own templates folder.  So a
package is a tiny manifest plus the document::

    <package>/package.json      what this is, which files it holds, and their sha256
    <package>/template.json     the ``wv-template@1`` document itself

The manifest is *closed*: an extra file in the folder is refused rather than ignored,
because "a script nobody declared" is exactly what a package must not be able to hide.
Importing re-reads the document through the normal strict reader and then goes through
:meth:`TemplateStore.save`, so a version that already exists is refused and an imported
version is never rewritten — the same rule as a locally written one.

Layout is flat on purpose: the document names no font, no voice and no path (those come
from the resolution chain), which is what makes a copy work at another root.
"""
from pathlib import Path
import hashlib
import re

from ..application.templates import SCHEMA as TEMPLATE_SCHEMA, TemplateDocument
from ..domain.errors import SchemaError
from .project_store import read_document, write_document
from .templates import checked_id

PACKAGE_SCHEMA = 'wv-template-package@1'
MANIFEST_FILENAME = 'package.json'
DOCUMENT_FILENAME = 'template.json'
PACKAGE_KEYS = ('schema', 'template_id', 'version', 'note', 'created', 'files')

#: Suffixes a template package may never contain.  A package is data; anything that a
#: shell, an interpreter or an installer would execute is refused by name.
SCRIPT_SUFFIXES = ('.py', '.pyc', '.pyo', '.pyw', '.bat', '.cmd', '.ps1', '.psm1',
                   '.sh', '.bash', '.zsh', '.exe', '.dll', '.com', '.scr', '.msi',
                   '.jar', '.js', '.mjs', '.vbs', '.vbe', '.wsf', '.wsh', '.hta',
                   '.lnk', '.reg', '.scf', '.inf')
#: The one member a package may contain, besides the manifest.
DATA_SUFFIXES = ('.json', '.txt', '.md')
_SECRET_KEY = re.compile(r'(pass(word|wd)?|secret|token|api[_-]?key|access[_-]?key|'
                         r'credential|private[_-]?key|bearer|authorization|'
                         r'app[_-]?(id|secret)|session[_-]?id)', re.I)
#: A credential *anywhere* in a string: a paste of "token: sk-…" into a note is the
#: realistic accident, and the prefixes are specific enough not to fire on prose.
_SECRET_VALUE = re.compile(r'(?:sk-|AKLT|gh[pousr]_|xox[baprs]-|AIza)[A-Za-z0-9_\-]{6,}')
#: A Windows path anywhere in a string — a drive letter (or a UNC share) at the start of
#: a word.  Deliberately not "any ``x:/``": a URL's scheme is a letter followed by a
#: slash too, and refusing ``https://…`` inside a sentence would be a false positive.
_ABSOLUTE = re.compile(r'(?:^|[\s(\[{"\':,，、（【])'
                       r'(?:[A-Za-z]:[\\/]|\\\\)')
_POSIX = re.compile(r'^/(?:[^/\s]+/)+')
_HEX_OR_BLOB = re.compile(r'^[A-Za-z0-9+/=_\-]{32,}$')


def _sha256(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b''):
            digest.update(chunk)
    return digest.hexdigest()


def document_scan(document, path='template'):
    """Everything a template document must not carry, as a list of problems.

    Reads every string in the document, whatever its shape, because a path or a key
    can hide inside a style value as easily as at the top level.  A problem is data
    (a code, where it is, what was seen) so a caller can print it and refuse.
    """
    problems, seen = [], []

    def walk(value, where):
        if isinstance(value, dict):
            for key, item in value.items():
                if isinstance(key, str) and _SECRET_KEY.search(key):
                    problems.append({'code': 'SECRET_FIELD', 'at': '%s.%s' % (where, key),
                                     'message': '模板里出现疑似凭据字段 %r' % key})
                walk(item, '%s.%s' % (where, key))
        elif isinstance(value, (list, tuple)):
            for index, item in enumerate(value):
                walk(item, '%s[%d]' % (where, index))
        elif isinstance(value, str):
            seen.append(value)
            if _ABSOLUTE.search(value) or _POSIX.match(value):
                problems.append({'code': 'ABSOLUTE_PATH', 'at': where,
                                 'message': '模板里有绝对路径：%r' % value})
            elif _SECRET_VALUE.search(value) or (_HEX_OR_BLOB.match(value)
                                                 and len(value) >= 40):
                problems.append({'code': 'SECRET_VALUE', 'at': where,
                                 'message': '模板里有疑似密钥的长串（不打印内容）'})

    walk(document, path)
    return problems, tuple(seen)


def folder_scan(folder):
    """The file-level scan: what is here, and which of it may not be here."""
    folder = Path(folder)
    problems, files, scripts = [], [], []
    for item in sorted(folder.rglob('*')):
        if item.is_dir():
            continue
        relative = str(item.relative_to(folder)).replace('\\', '/')
        files.append(relative)
        suffix = item.suffix.lower()
        if suffix in SCRIPT_SUFFIXES:
            scripts.append(relative)
            problems.append({'code': 'SCRIPT_IN_PACKAGE', 'at': relative,
                             'message': '模板包不得含可执行文件：%s' % relative})
        elif suffix not in DATA_SUFFIXES:
            problems.append({'code': 'UNKNOWN_FILE', 'at': relative,
                             'message': '模板包只放 package.json 与 template.json，'
                                        '多出：%s' % relative})
        head = item.open('rb').read(2)
        if head in (b'MZ', b'#!'):
            if relative not in scripts:
                scripts.append(relative)
            problems.append({'code': 'EXECUTABLE_CONTENT', 'at': relative,
                             'message': '文件内容是可执行体或脚本：%s' % relative})
    return {'folder': str(folder), 'files': files, 'scripts': scripts,
            'problems': problems, 'problem_count': len(problems)}


def build_package(document, *, created=''):
    """The manifest of one package: identity, note, and the file it will carry.

    The digest is filled in by :func:`write_package` from the bytes actually written,
    so the manifest describes the file on disk rather than a second serialization of
    the same document that could drift from it.
    """
    if not isinstance(document, TemplateDocument):
        raise SchemaError('a package carries a TemplateDocument', path='template')
    problems, _ = document_scan(document.to_dict())
    if problems:
        raise SchemaError('模板含有不能打包的内容：%s'
                          % '; '.join(item['message'] for item in problems),
                          path='template',
                          hint='模板包不得含绝对路径/凭据；字体与音色走本机解析链，不进模板')
    return {'schema': PACKAGE_SCHEMA, 'template_id': document.template_id,
            'version': document.version, 'note': document.note, 'created': created,
            'files': [{'path': DOCUMENT_FILENAME, 'sha256': '', 'bytes': 0}]}


def write_package(document, folder):
    """Write ``<folder>/package.json`` + ``template.json``; refuses a used folder."""
    folder = Path(folder)
    if folder.exists() and any(folder.iterdir()):
        raise SchemaError('the package folder is not empty: %s' % folder, path='template',
                          hint='换一个空目录，或先删掉里面的内容（导出不覆盖已有包）')
    manifest = build_package(document)
    folder.mkdir(parents=True, exist_ok=True)
    document_path = write_document(document.to_dict(), folder / DOCUMENT_FILENAME)
    manifest['files'] = [{'path': DOCUMENT_FILENAME,
                          'sha256': _sha256(document_path),
                          'bytes': document_path.stat().st_size}]
    manifest_path = write_document(manifest, folder / MANIFEST_FILENAME)
    return {'folder': str(folder), 'manifest': str(manifest_path),
            'document': str(document_path), 'template': '%s@%d'
            % (document.template_id, document.version)}


def read_package(folder):
    """``(document, report)`` for one package folder; refuses an unclean one.

    The manifest is checked against the folder both ways — every declared file must
    be there with its digest, and no undeclared file may exist — before the document
    is parsed, so a package that was edited after export cannot be imported as if it
    were the exported one.
    """
    folder = Path(folder)
    if not folder.is_dir():
        raise SchemaError('no template package at %s' % folder, path='template')
    manifest = read_document(folder / MANIFEST_FILENAME, 'template package')
    unknown = sorted(set(manifest) - set(PACKAGE_KEYS))
    if unknown:
        raise SchemaError('package.json has unknown field(s): %s' % ', '.join(unknown),
                          path='template')
    if manifest.get('schema') != PACKAGE_SCHEMA:
        raise SchemaError('unknown package schema %r' % (manifest.get('schema'),),
                          path='template', hint='本版本只读 %s' % PACKAGE_SCHEMA)
    template_id = checked_id(manifest.get('template_id', ''))
    version = manifest.get('version')
    if isinstance(version, bool) or not isinstance(version, int) or version <= 0:
        raise SchemaError('package version must be a positive integer', path='template')
    declared = {}
    for item in manifest.get('files') or ():
        if not isinstance(item, dict) or 'path' not in item:
            raise SchemaError('every package file entry needs a path', path='template')
        declared[str(item['path']).replace('\\', '/')] = item
    present = sorted(str(path.relative_to(folder)).replace('\\', '/')
                     for path in folder.rglob('*') if path.is_file())
    problems = []
    for name in present:
        if name == MANIFEST_FILENAME:
            continue
        if name not in declared:
            problems.append({'code': 'UNDECLARED_FILE', 'at': name,
                             'message': 'package.json 没有声明这个文件：%s' % name})
    for name, item in sorted(declared.items()):
        path = folder / name
        if not path.is_file():
            problems.append({'code': 'DECLARED_FILE_MISSING', 'at': name,
                             'message': 'package.json 声明了但文件不在：%s' % name})
            continue
        if item.get('sha256') and _sha256(path) != item['sha256']:
            problems.append({'code': 'DIGEST_MISMATCH', 'at': name,
                             'message': '文件与 package.json 的 sha256 不一致：%s' % name})
        if item.get('bytes') is not None and path.stat().st_size != item['bytes']:
            problems.append({'code': 'SIZE_MISMATCH', 'at': name,
                             'message': '文件大小与 package.json 不一致：%s' % name})
    scan = folder_scan(folder)
    problems.extend(scan['problems'])
    if problems:
        raise SchemaError(
            '模板包不可导入：%s' % '; '.join(item['message'] for item in problems[:4]),
            path='template',
            hint='重新导出一份干净的包；包内只允许 package.json 与 template.json')
    document = TemplateDocument.from_dict(
        read_document(folder / DOCUMENT_FILENAME, 'template'), 'template')
    if document.template_id != template_id or document.version != version:
        raise SchemaError(
            'package.json says %s@%d but template.json says %s@%d'
            % (template_id, version, document.template_id, document.version),
            path='template', hint='包里的两个文件必须说同一个身份')
    found, _ = document_scan(document.to_dict())
    if found:
        raise SchemaError('模板包里的文档不可导入：%s'
                          % '; '.join(item['message'] for item in found[:4]),
                          path='template',
                          hint='导入方只接受不含绝对路径与凭据的模板')
    return document, {'folder': str(folder), 'template_id': template_id,
                      'version': version, 'files': present, 'scan': scan,
                      'schema': TEMPLATE_SCHEMA}


def import_package(folder, store):
    """Read one package and save its version under ``store``'s root.

    Never overwrites: an existing version is refused by ``TemplateStore.save``, which
    is the same rule that keeps "this batch used X@2" true (a version is a fact once it
    is referenced, not a mutable file).
    """
    document, report = read_package(folder)
    path = store.save(document)
    return {**report, 'imported': str(path), 'template': '%s@%d'
            % (document.template_id, document.version)}
