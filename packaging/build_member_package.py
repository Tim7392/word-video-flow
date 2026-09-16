"""Build the member-facing directory package for the word-video engine.

What it produces (one folder, copied to a member machine as-is):

    word-video-member-<version>/
      WordVideo/                PyInstaller onedir build of word_video_cli.py
      ffmpeg/ffmpeg.exe         ffmpeg + ffprobe copied from the development machine
      ffmpeg/ffprobe.exe
      单词视频.cmd               launcher: package-internal TEMP, PATH gains only ffmpeg
      README-成员使用.md
      VERSION.txt               version, build time, source commit, file count/size

The entry point stays the existing headless CLI, so argv and the single-JSON
stdout contract are unchanged.  The only frozen-mode difference (the worker argv
that ``word_video_cli.py start`` injects through ``sys.executable``) is handled
by ``packaging/pyi_rth_cli_argv.py``; production code is not touched.

Run it with the locked interpreter - it needs ``-m PyInstaller`` from that venv:

    & D:\\1\\1-AI_workflow\\word_video_flow\\runtime\\venv\\Scripts\\python.exe `
        packaging\\build_member_package.py

The package is assembled under a hidden ``.building-*`` staging name and renamed
into place only after the content scan passes, so a failed build never leaves a
half package next to a working one.  The scan is also the evidence for "no keys,
no real media, no audio cache, no full word list, no font without a distribution
basis": its JSON goes to stdout and its verdict into VERSION.txt.

    --scan <folder>   scan an already built package and exit (no build)
"""
import argparse
import hashlib
import importlib.metadata
import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

APP_DIR = 'WordVideo'
APP_EXE = 'WordVideo.exe'
FFMPEG_DIR = 'ffmpeg'
FFMPEG_TOOLS = ('ffmpeg.exe', 'ffprobe.exe')
ENTRY_SCRIPT = 'word_video_cli.py'
RUNTIME_HOOK = 'pyi_rth_cli_argv.py'
LAUNCHER_NAME = '单词视频.cmd'
README_NAME = 'README-成员使用.md'
VERSION_NAME = 'VERSION.txt'
VERSION_SOURCE = 'member_version.txt'
JYD_PACKAGE = 'pyJianYingDraft'
MEDIAINFO_DLL = 'MediaInfo.dll'
CHANNEL = 'w09-early-package'
PACKAGE_PREFIX = 'word-video-member-'
FIXTURE_NAME = 'p1-无片头.json'

#: Imported by the app but never part of the member package: the Jianying UI
#: automation route stays out of the packaged tool, and the excluded scientific /
#: imaging / test stacks would only inflate it.
EXCLUDED_MODULES = ('uiautomation', 'comtypes', 'pyautogui', 'PyQt5', 'PyQt6', 'PySide2',
                    'PySide6', 'numpy', 'PIL', 'imageio', 'pytest', '_pytest')

#: Files that must never ship.  Suffix list first, header bytes second: an
#: extension can lie, a file header cannot.
FORBIDDEN_SUFFIXES = {
    '.mp4': 'video material', '.mov': 'video material', '.mkv': 'video material',
    '.webm': 'video material', '.avi': 'video material', '.wav': 'audio material',
    '.mp3': 'audio material', '.ogg': 'audio material', '.m4a': 'audio material',
    '.flac': 'audio material', '.aac': 'audio material', '.wma': 'audio material',
    '.docx': 'document material', '.doc': 'document material',
    '.ttf': 'font without a distribution basis', '.otf': 'font without a distribution basis',
    '.ttc': 'font without a distribution basis', '.woff': 'font without a distribution basis',
    '.woff2': 'font without a distribution basis',
    '.srt': 'product output', '.sqlite3': 'job database', '.log': 'run log', '.lock': 'job lock'}

FONT_MAGIC = ((b'\x00\x01\x00\x00', 'TrueType font'), (b'OTTO', 'OpenType/CFF font'),
              (b'ttcf', 'TrueType collection'), (b'true', 'TrueType font'),
              (b'wOFF', 'WOFF font'), (b'wOF2', 'WOFF2 font'))

MEDIA_MAGIC = ((b'OggS', 'Ogg audio'), (b'RIFF', 'RIFF container (WAV/AVI)'),
               (b'ID3', 'MP3 audio'), (b'\x1aE\xdf\xa3', 'Matroska/WebM'),
               (b'fLaC', 'FLAC audio'), (b'FORM', 'AIFF audio'))

#: A library shipping its own data is not "a member's material".  Every allowance
#: is explicit, and is reported in the scan evidence instead of being skipped.
ALLOWANCES = (
    (re.compile(r'^WordVideo/_internal/docx/templates/[^/]+\.docx$'),
     'blank-document template of the python-docx library itself'),
)

FORBIDDEN_PATH_PARTS = ('audio-cache', 'word_video_work', 'recovery', 'jobs.sqlite3')

SECRET_LITERAL_PATTERNS = (
    (re.compile(rb'(?i)(api[_-]?key|access[_-]?token|secret|password|appid|app[_-]?secret)'
                rb'\s*[:=]\s*[\'"][A-Za-z0-9+/=_.\-]{16,}[\'"]'),
     'credential-shaped literal'),
    (re.compile(rb'(?i)bearer\s+[A-Za-z0-9\-_.=]{20,}'), 'bearer token literal'),
)

#: Environment names whose *values* must not appear anywhere in the package.
SECRET_ENV_NAMES = ('VOLC_TTS_APPID', 'VOLC_TTS_ACCESS_TOKEN', 'VOLC_TTS_CLUSTER',
                    'VOLC_TTS_API_KEY', 'VOLC_TTS_SECRET_KEY', 'MODEL_SPEECH_API_KEY')

DEFAULT_DEV_MARKERS = ('PycharmProjects', 'runtime\\venv', 'runtime/venv')

#: "word /phonetic/ ..." is the shape of a vocabulary list.  The phonetic part must
#: contain a non-ASCII (IPA) character, which is what keeps code such as
#: ``items [i]`` from being counted as a word list.
WORDLIST_SHAPE = re.compile(r"^[A-Za-z][A-Za-z'\-]*(?:\s+[A-Za-z'\-]+){0,2}\s+[/\[][^\]/\n]{1,60}[/\]]")
WORDLIST_LINE_LIMIT = 100

TEXT_HEAD = 8192
MAX_TEXT_BYTES = 8 * 1024 * 1024
MAX_BINARY_SCAN_BYTES = 64 * 1024 * 1024


def repo_root():
    return Path(__file__).resolve().parents[1]


def find_work_root(root):
    """The work root above this checkout.

    A work root is recognised by its ``runtime/`` folder *and* one of the folders
    that only exist there (``out/``, ``repo/``, ``worktrees/``).  ``runtime`` alone
    is not enough: a stray ``runtime`` folder next to the role worktrees once made
    this walk stop one level too early, which then sent build and verify scratch
    into the wrong tree.
    """
    root = Path(root).resolve()
    for candidate in [root] + list(root.parents)[:3]:
        if (candidate / 'runtime').is_dir() and any(
                (candidate / name).is_dir() for name in ('out', 'repo', 'worktrees')):
            return candidate
    return None


def default_out_root(root):
    """`<work root>/out/packages`, else `<repo>/dist/packages` for a loose copy."""
    work_root = find_work_root(root)
    return (work_root or Path(root).parent) / 'out' / 'packages'


def default_tmp_root(root, out_root):
    work_root = find_work_root(root)
    if work_root is not None and (work_root / 'runtime').is_dir():
        return work_root / 'runtime' / 'tmp' / Path(root).name
    return Path(out_root) / '.tmp'


def default_ffmpeg_dir():
    return Path(os.environ.get('WORD_VIDEO_FFMPEG_DIR') or (Path.home() / '.local' / 'bin'))


def read_version(root, override=None):
    if override:
        return override
    text = (Path(root) / 'packaging' / VERSION_SOURCE).read_text(encoding='utf-8').strip()
    if not re.fullmatch(r'[0-9A-Za-z][0-9A-Za-z._\-]{0,31}', text):
        raise SystemExit('bad version in %s: %r' % (VERSION_SOURCE, text))
    return text


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def tree_size(root, exclude=()):
    """(file count, total bytes) below ``root``, ignoring ``exclude`` paths."""
    skip = {Path(item) for item in exclude}
    count = total = 0
    for path in sorted(Path(root).rglob('*')):
        if path.is_file() and path not in skip:
            count += 1
            total += path.stat().st_size
    return count, total


def git_identity(root):
    """HEAD plus an explicit dirty *summary*, never a bare boolean.

    A package whose ``VERSION.txt`` says only ``source_dirty=true`` cannot be tied
    to a revision: a later reader knows it differs from the recorded commit but not
    how, so the package's verification can only ever be about that one folder.  The
    changed paths are recorded (bounded) so the difference can be reasoned about -
    and a clean build, which is the normal case, records none.
    """
    def run(*args):
        result = subprocess.run(['git', '-C', str(root)] + list(args), capture_output=True,
                                text=True, encoding='utf-8', errors='replace')
        return result.stdout.strip() if result.returncode == 0 else ''

    status = run('status', '--porcelain').lstrip('\ufeff')
    # Split on whitespace rather than slicing at a fixed offset: a leading BOM or a
    # future porcelain tweak would otherwise silently eat the first character of the
    # first path, and a truncated path in the evidence is worse than none.
    changed = [parts[1].strip() for parts in
               (line.split(None, 1) for line in status.splitlines() if line.strip())
               if len(parts) > 1]
    head = run('rev-parse', 'HEAD') or 'unknown'
    return {'commit': head, 'branch': run('rev-parse', '--abbrev-ref', 'HEAD'),
            'dirty': bool(changed),
            'dirty_count': len(changed), 'dirty_files': changed[:25]}


def locate_installed(distribution, package):
    """Where the build interpreter's site-packages keeps ``package``."""
    dist = importlib.metadata.distribution(distribution)
    path = Path(dist.locate_file(package))
    if not path.is_dir():
        raise SystemExit('%s: %s is not a directory' % (distribution, path))
    return path


def stage_build_data(tmp_root, jyd_source, mediainfo_dll):
    """Copy the two pieces PyInstaller cannot infer by static analysis.

    ``pyJianYingDraft`` is reached only through a runtime ``importlib`` lookup
    (``word_video/draft.py`` loads its pure submodules from the real folder), so its
    sources must exist as files instead of living inside the PYZ archive.
    ``MediaInfo.dll`` must sit next to the ``pymediainfo`` module, because that is
    where ``pymediainfo`` looks for it.
    """
    data = Path(tmp_root) / 'build-data'
    if data.exists():
        shutil.rmtree(data)
    jyd_target = data / JYD_PACKAGE
    shutil.copytree(jyd_source, jyd_target, ignore=shutil.ignore_patterns('__pycache__'))
    media_target = data / 'pymediainfo'
    media_target.mkdir(parents=True)
    shutil.copy2(mediainfo_dll, media_target / Path(mediainfo_dll).name)
    return {'jyd': jyd_target, 'media': media_target}


def pyinstaller_command(root, hook, jyd_dir, media_dir, dist, work, spec, python=None):
    root = Path(root)
    command = [str(python or sys.executable), '-m', 'PyInstaller',
               '--noconfirm', '--onedir', '--console', '--noupx',
               '--name', APP_DIR,
               '--distpath', str(dist), '--workpath', str(work), '--specpath', str(spec),
               '--paths', str(root),
               '--runtime-hook', str(hook),
               '--copy-metadata', JYD_PACKAGE,
               '--add-data', '%s%s%s' % (jyd_dir, os.pathsep, JYD_PACKAGE),
               '--add-data', '%s%spymediainfo' % (media_dir, os.pathsep),
               '--hidden-import', 'pymediainfo',
               '--hidden-import', 'docx',
               '--collect-submodules', 'docx']
    for name in EXCLUDED_MODULES:
        command += ['--exclude-module', name]
    command.append(str(root / ENTRY_SCRIPT))
    return command


def package_layout_problems(package):
    """Entries the delivered folder must have, as package-relative posix paths."""
    package = Path(package)
    required = ['%s/%s' % (APP_DIR, APP_EXE),
                '%s/%s' % (FFMPEG_DIR, FFMPEG_TOOLS[0]),
                '%s/%s' % (FFMPEG_DIR, FFMPEG_TOOLS[1]),
                LAUNCHER_NAME, README_NAME, VERSION_NAME]
    return [item for item in required if not (package / item).is_file()]


def wordlist_lines(text):
    count = 0
    for line in text.splitlines():
        match = WORDLIST_SHAPE.match(line)
        if match and any(ord(character) > 127 for character in match.group(0)):
            count += 1
    return count


def allowance_for(relative):
    for pattern, reason in ALLOWANCES:
        if pattern.match(relative):
            return reason
    return None


def looks_like_text(head):
    return b'\x00' not in head


def scan_package(package, guard_files=(), env_values=(), dev_markers=(), exclude=(),
                 wordlist_limit=WORDLIST_LINE_LIMIT, max_findings=40):
    """Look for what must not be in a member package; report, never repair."""
    package = Path(package).resolve()
    excluded = set(exclude)
    findings = []
    secrets = [(name, value.encode('utf-8')) for name, value in env_values
               if value and len(value) >= 8]
    markers = [marker.encode('utf-8') for marker in (tuple(dev_markers) or DEFAULT_DEV_MARKERS)]
    guards = []
    for item in guard_files:
        path = Path(item)
        if path.is_file():
            guards.append((path.stat().st_size, sha256_file(path), path.name))
    evidence = {'files': 0, 'bytes': 0, 'text_files': 0, 'magic_checked': 0,
                'guard_files': len(guards), 'max_wordlist_lines': 0,
                'allowances': {}, 'excluded': sorted(excluded)}

    def report(rule, severity, relative, detail):
        findings.append({'rule': rule, 'severity': severity, 'path': relative, 'detail': detail})

    for path in sorted(package.rglob('*')):
        if not path.is_file():
            continue
        relative = path.relative_to(package).as_posix()
        if relative in excluded:
            continue
        size = path.stat().st_size
        evidence['files'] += 1
        evidence['bytes'] += size
        with path.open('rb') as stream:
            head = stream.read(TEXT_HEAD)
        text_file = looks_like_text(head)
        if text_file:
            evidence['text_files'] += 1
        allowed = allowance_for(relative)
        if allowed:
            evidence['allowances'][relative] = allowed

        suffix = path.suffix.lower()
        if suffix in FORBIDDEN_SUFFIXES and not allowed:
            report('forbidden-extension', 'error', relative,
                   '%s (%s)' % (FORBIDDEN_SUFFIXES[suffix], suffix))
        for part in FORBIDDEN_PATH_PARTS:
            if part in relative and not allowed:
                report('forbidden-path-part', 'error', relative, 'path contains %r' % part)
                break

        evidence['magic_checked'] += 1
        for magic, kind in FONT_MAGIC:
            if head.startswith(magic) and not allowed:
                report('font-magic', 'error', relative, kind)
                break
        if head[4:8] == b'ftyp' and not allowed:
            report('media-magic', 'error', relative, 'ISO base media (MP4/MOV)')
        for magic, kind in MEDIA_MAGIC:
            if head.startswith(magic) and not allowed:
                report('media-magic', 'error', relative, kind)
                break

        for size_guard, digest, source in guards:
            if size == size_guard and sha256_file(path) == digest and not allowed:
                report('guard-file', 'error', relative,
                       'byte-identical to a protected source file (%s)' % source)
                break

        if text_file and 0 < size <= MAX_TEXT_BYTES:
            text = path.read_text(encoding='utf-8', errors='replace')
            lines = wordlist_lines(text)
            evidence['max_wordlist_lines'] = max(evidence['max_wordlist_lines'], lines)
            if lines >= wordlist_limit and not allowed:
                report('wordlist-shape', 'error', relative,
                       '%d vocabulary-list shaped lines' % lines)

        for name, value in secrets:
            with path.open('rb') as stream:
                if value in stream.read():
                    report('secret-env-value', 'error', relative,
                           'contains the value of %s' % name)

        if text_file:
            with path.open('rb') as stream:
                payload = stream.read()
            for pattern, label in SECRET_LITERAL_PATTERNS:
                if pattern.search(payload) and not allowed:
                    report('secret-literal', 'error', relative, label)
                    break
            for marker in markers:
                if marker in payload and not allowed:
                    report('dev-path', 'error', relative,
                           'development path %r in a text file'
                           % marker.decode('utf-8', 'replace'))
                    break
        elif 0 < size <= MAX_BINARY_SCAN_BYTES:
            with path.open('rb') as stream:
                payload = stream.read()
            for marker in markers:
                if marker in payload:
                    report('dev-path', 'warning', relative,
                           'development path %r inside a binary'
                           % marker.decode('utf-8', 'replace'))
                    break

    errors = sum(1 for item in findings if item['severity'] == 'error')
    warnings = sum(1 for item in findings if item['severity'] == 'warning')
    return {'package': str(package), 'errors': errors, 'warnings': warnings,
            'findings': findings[:max_findings], 'finding_count': len(findings),
            'evidence': evidence}


def render_readme(template, facts):
    """Fill the member-facing readme; it must not carry template placeholders."""
    text = template
    for name, value in (('{{VERSION}}', facts['version']), ('{{BUILT}}', facts['built']),
                        ('{{COMMIT}}', facts['commit'])):
        text = text.replace(name, str(value))
    if '{{' in text:
        raise SystemExit('unresolved placeholder in %s' % README_NAME)
    return text


def write_readme(package, template, facts):
    path = Path(package) / README_NAME
    path.write_text(render_readme(Path(template).read_text(encoding='utf-8'), facts),
                    encoding='utf-8')
    return path


def render_version(facts, file_count, byte_count):
    lines = ['package=%s%s' % (PACKAGE_PREFIX, facts['version']),
             'version=%s' % facts['version'],
             'channel=%s' % CHANNEL,
             'built=%s' % facts['built'],
             'source_commit=%s' % facts['commit'],
             'source_branch=%s' % (facts.get('branch') or 'unknown'),
             'source_dirty=%s' % str(facts['dirty']).lower(),
             'source_dirty_count=%d' % facts.get('dirty_count', 0),
             # Named, not just flagged: a bare "dirty" cannot be tied to a revision.
             'source_dirty_files=%s' % (', '.join(facts.get('dirty_files') or [])
                                        or 'none'),
             'built_with=%s' % facts['built_with'],
             'entry=%s (existing headless JSON CLI; argv and stdout contract unchanged)'
             % ENTRY_SCRIPT,
             'member_requirements=none (no Python, no FFmpeg, no PATH change)',
             'temp=%PKG%\\temp unless WORD_VIDEO_TEMP is set',
             'layout=%s/ (PyInstaller onedir), %s/, %s, %s, %s'
             % (APP_DIR, FFMPEG_DIR, LAUNCHER_NAME, README_NAME, VERSION_NAME)]
    lines += ['%s=%s' % item for item in sorted(facts['tools'].items())]
    lines += ['content_scan=%s errors=%d warnings=%d (every file except %s itself)'
              % (facts['scan']['result'], facts['scan']['errors'], facts['scan']['warnings'],
                 VERSION_NAME),
              'files=%d' % file_count,
              'bytes=%d' % byte_count]
    return '\n'.join(lines) + '\n'


def write_version_file(package, facts):
    """Write VERSION.txt, settling the file count/size it reports about itself."""
    path = Path(package) / VERSION_NAME
    count, size = tree_size(package, exclude=(path,))
    length = 0
    payload = b''
    for _ in range(6):
        payload = render_version(facts, count + 1, size + length).encode('utf-8')
        if len(payload) == length:
            break
        length = len(payload)
    path.write_bytes(payload)
    return path


def guard_files(root, fixture_name=FIXTURE_NAME):
    """Real inputs whose bytes must never reappear inside the package.

    Everything under ``data/`` (fixtures, word lists, job databases) plus the media
    the three-word fixture references.  Compared by size first, so the 1080p
    background is never hashed unless a packaged file happens to match its size.
    """
    root = Path(root)
    guards = []
    data = (find_work_root(root) or root.parent) / 'data'
    if data.is_dir():
        guards += [path for path in sorted(data.rglob('*')) if path.is_file()]
    try:
        request = json.loads((data / 'fixtures' / fixture_name).read_text(encoding='utf-8-sig'))
    except (OSError, ValueError):
        return guards
    for asset in (request.get('provider') or {}).get('items', []):
        guards.append(Path(asset['path']))
    for key in ('background', 'intro'):
        value = (request.get('lesson') or {}).get(key)
        if isinstance(value, str):
            guards.append(Path(value))
    source = (request.get('source') or {}).get('path')
    if source:
        guards.append(Path(source))
    return guards


def dev_markers(root):
    """Absolute paths that only exist on a development machine."""
    root = Path(root).resolve()
    work_root = find_work_root(root) or root.parent
    return (str(root), str(root).replace('\\', '/'), str(work_root),
            str(work_root).replace('\\', '/'))


def ffmpeg_version(path):
    result = subprocess.run([str(path), '-version'], capture_output=True, text=True,
                            encoding='utf-8', errors='replace')
    lines = (result.stdout or result.stderr).splitlines()
    return lines[0].strip() if lines else 'unknown'


def build(args):
    root = repo_root()
    version = read_version(root, args.version)
    out_root = Path(args.out).resolve() if args.out else default_out_root(root)
    tmp_root = Path(args.tmp).resolve() if args.tmp else default_tmp_root(root, out_root)
    final = out_root / (PACKAGE_PREFIX + version)
    stage = out_root / ('.building-%s%s' % (PACKAGE_PREFIX, version))
    started = time.monotonic()
    steps = []

    def note(message):
        steps.append(message)
        print('[build] %s' % message, file=sys.stderr, flush=True)

    if final.exists() and not args.force:
        raise SystemExit('%s already exists; pass --force to replace it' % final)
    launcher_template = Path('packaging') / 'launcher' / LAUNCHER_NAME
    readme_template = Path('packaging') / README_NAME
    hook_source = Path('packaging') / RUNTIME_HOOK
    for relative in (Path(ENTRY_SCRIPT), hook_source, launcher_template, readme_template):
        if not (root / relative).is_file():
            raise SystemExit('missing source file: %s' % (root / relative))
    ffmpeg_dir = Path(args.ffmpeg_dir).resolve() if args.ffmpeg_dir else default_ffmpeg_dir()
    for tool in FFMPEG_TOOLS:
        if not (ffmpeg_dir / tool).is_file():
            raise SystemExit('missing %s in %s' % (tool, ffmpeg_dir))
    jyd_source = locate_installed(JYD_PACKAGE, JYD_PACKAGE)
    mediainfo = locate_installed('pymediainfo', 'pymediainfo') / MEDIAINFO_DLL
    if not mediainfo.is_file():
        raise SystemExit('missing %s next to the pymediainfo module' % mediainfo)

    tmp_root.mkdir(parents=True, exist_ok=True)
    out_root.mkdir(parents=True, exist_ok=True)
    if stage.exists():
        if not stage.name.startswith('.building-'):
            raise SystemExit('refusing to remove %s' % stage)
        shutil.rmtree(stage)
    stage.mkdir()

    note('version %s -> %s' % (version, final))
    note('ffmpeg source %s' % ffmpeg_dir)
    note('staging runtime data (pyJianYingDraft sources + %s)' % MEDIAINFO_DLL)
    data = stage_build_data(tmp_root, jyd_source, mediainfo)
    shutil.copy2(root / launcher_template, stage / LAUNCHER_NAME)
    (stage / FFMPEG_DIR).mkdir()
    for tool in FFMPEG_TOOLS:
        shutil.copy2(ffmpeg_dir / tool, stage / FFMPEG_DIR / tool)

    dist, work, spec = tmp_root / 'dist', tmp_root / 'work', tmp_root / 'spec'
    if args.clean:
        shutil.rmtree(work, ignore_errors=True)
    command = pyinstaller_command(root, root / hook_source,
                                  data['jyd'], data['media'], dist, work, spec)
    note('running: %s' % ' '.join(command[:6] + ['...']))
    environment = dict(os.environ)
    environment['PYINSTALLER_CONFIG_DIR'] = str(tmp_root / 'pyinstaller-config')
    result = subprocess.run(command, cwd=str(root), env=environment, capture_output=True,
                            text=True, encoding='utf-8', errors='replace')
    pyi_log = tmp_root / 'pyinstaller.log'
    pyi_log.write_text(result.stdout + result.stderr, encoding='utf-8')
    if result.returncode:
        print('\n'.join((result.stdout + result.stderr).splitlines()[-40:]), file=sys.stderr)
        raise SystemExit('PyInstaller failed (%d); full log: %s' % (result.returncode, pyi_log))
    warn_file = work / APP_DIR / ('warn-%s.txt' % APP_DIR)
    unresolved = [line for line in warn_file.read_text(encoding='utf-8', errors='replace').splitlines()
                  if line.startswith('missing module named')] if warn_file.is_file() else []
    note('PyInstaller done (%d modules it could not find; log %s)' % (len(unresolved), pyi_log))
    built_app = dist / APP_DIR
    if not (built_app / APP_EXE).is_file():
        raise SystemExit('PyInstaller produced no %s in %s' % (APP_EXE, built_app))
    shutil.move(str(built_app), str(stage / APP_DIR))

    layout_missing = [item for item in package_layout_problems(stage)
                      if item not in (VERSION_NAME, README_NAME)]
    if layout_missing:
        raise SystemExit('PyInstaller output is incomplete: missing %s' % ', '.join(layout_missing))
    frozen = stage / APP_DIR
    for required in ('_internal/%s/__init__.py' % JYD_PACKAGE,
                     '_internal/pymediainfo/%s' % MEDIAINFO_DLL,
                     '_internal/pyjianyingdraft-0.3.0.dist-info/METADATA'):
        if not (frozen / required).is_file():
            raise SystemExit('frozen app is missing %s' % required)
    note('layout and runtime data verified')

    facts = {'version': version,
             'built': datetime.now().astimezone().strftime('%Y-%m-%d %H:%M:%S %z'),
             'built_with': 'CPython %s / PyInstaller %s'
                           % (sys.version.split()[0], importlib.metadata.version('PyInstaller')),
             'tools': {'ffmpeg_version': ffmpeg_version(ffmpeg_dir / 'ffmpeg.exe'),
                       'ffmpeg_sha256': sha256_file(ffmpeg_dir / 'ffmpeg.exe'),
                       'ffprobe_sha256': sha256_file(ffmpeg_dir / 'ffprobe.exe'),
                       'mediainfo_dll_sha256': sha256_file(mediainfo)}}
    facts.update(git_identity(root))
    if facts['dirty']:
        note('WARNING: building from a dirty worktree; %d changed path(s) will be '
             'named in %s: %s' % (facts['dirty_count'], VERSION_NAME,
                                  ', '.join(facts['dirty_files'][:5])))
    write_readme(stage, root / readme_template, facts)

    scan = scan_package(stage,
                        guard_files=guard_files(root),
                        env_values=[(name, os.environ.get(name, '')) for name in SECRET_ENV_NAMES],
                        dev_markers=dev_markers(root),
                        exclude=(VERSION_NAME,))
    scan['result'] = 'PASS' if not scan['errors'] else 'FAIL'
    facts['scan'] = scan
    note('content scan %s (errors=%d warnings=%d, %d files / %d bytes)'
         % (scan['result'], scan['errors'], scan['warnings'],
            scan['evidence']['files'], scan['evidence']['bytes']))

    write_version_file(stage, facts)
    layout_missing = package_layout_problems(stage)
    if layout_missing:
        raise SystemExit('staged package is incomplete: missing %s' % ', '.join(layout_missing))

    replaced = None
    if scan['errors']:
        note('content scan FAILED; staging folder kept at %s' % stage)
    else:
        if final.exists():
            replaced = final.with_name(final.name + '.replaced-%s' % time.strftime('%Y%m%d-%H%M%S'))
            final.rename(replaced)
            note('previous package moved aside to %s' % replaced.name)
        stage.rename(final)
        if replaced:
            shutil.rmtree(replaced)
        note('published %s' % final)

    location = stage if scan['errors'] else final
    count, size = tree_size(location)
    return {'ok': not scan['errors'], 'published': not scan['errors'],
            'package': str(location), 'version': version,
            'files': count, 'bytes': size, 'seconds': round(time.monotonic() - started, 1),
            'pyinstaller_missing_modules': len(unresolved),
            'pyinstaller_warn_file': str(warn_file), 'pyinstaller_log': str(pyi_log),
            'scan': scan, 'steps': steps}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--version', help='override packaging/%s' % VERSION_SOURCE)
    parser.add_argument('--out', help='output root (default: <work root>/out/packages)')
    parser.add_argument('--tmp', help='build temp root (default: <work root>/runtime/tmp/<role>)')
    parser.add_argument('--ffmpeg-dir', help='folder holding ffmpeg.exe and ffprobe.exe')
    parser.add_argument('--force', action='store_true', help='replace an existing package')
    parser.add_argument('--clean', action='store_true', help='drop the PyInstaller work folder')
    parser.add_argument('--scan', help='scan an existing package and exit')
    args = parser.parse_args(argv)
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    if args.scan:
        package = Path(args.scan).resolve()
        root = repo_root()
        scan = scan_package(package,
                            guard_files=guard_files(root),
                            env_values=[(name, os.environ.get(name, ''))
                                        for name in SECRET_ENV_NAMES],
                            dev_markers=dev_markers(root))
        scan['result'] = 'PASS' if not scan['errors'] else 'FAIL'
        scan['layout_missing'] = package_layout_problems(package)
        scan['ok'] = scan['result'] == 'PASS' and not scan['layout_missing']
        print(json.dumps(scan, ensure_ascii=False, indent=1))
        return 0 if scan['ok'] else 1
    report = build(args)
    print(json.dumps(report, ensure_ascii=False, indent=1))
    return 0 if report['ok'] else 1


if __name__ == '__main__':
    sys.exit(main())
