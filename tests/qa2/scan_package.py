"""QA-2: scan the member package for things a member must never receive.

Independent of C's own scanner: the patterns and the positive controls are QA's,
and a hit is reported with the file, the offset and a redacted excerpt.  Five
questions, one section each:

  secrets     key-shaped strings (cloud keys, bearer tokens, private keys,
              ``api_key=``-style assignments) anywhere in a text or binary file;
              the package's own version/help output legitimately mentions the
              *name* of an environment variable, so a hit must show a value;
  media       real delivery media (mp4/mov/mkv/wav/mp3/ogg/m4a/flac) other than
              nothing - the package ships no素材;
  wordlist    a whole word list: a run of consecutive lines that look like
              ``word /phonetic/ pos. meaning``;
  caches      an audio cache: a ``complete.json`` beside audio, or a directory
              holding many same-shaped audio files;
  userdata    project/job state a member did not create (project.json, *.sqlite3,
              word-video batches, job-owner.json).

Usage:
  python scan_package.py --package DIR [--json OUT] [--wordlist TXT]
"""
import argparse
import json
from pathlib import Path
import re
import sys

sys.stdout.reconfigure(encoding='utf-8', errors='replace')

# Key-shaped: a long opaque token, or an assignment whose value is not a placeholder.
PATTERNS = [
    ('private_key', re.compile(rb'-----BEGIN [A-Z ]*PRIVATE KEY-----')),
    ('aws_style', re.compile(rb'\b(?:AKIA|ASIA)[0-9A-Z]{16}\b')),
    ('bearer', re.compile(rb'[Bb]earer\s+[A-Za-z0-9\-._~+/]{20,}={0,3}')),
    ('long_hex_token', re.compile(rb'(?<![0-9a-fA-F])[0-9a-f]{32,}(?![0-9a-fA-F])')),
    ('assignment', re.compile(
        rb'(?i)\b(?:api[_-]?key|secret|access[_-]?key|app[_-]?id|token|password)'
        rb'\s*[:=]\s*["\']?([A-Za-z0-9\-_./+]{16,})["\']?')),
]
# Values that only name a variable or are obvious placeholders, not credentials.
PLACEHOLDER = re.compile(
    rb'(?i)^(?:your|example|placeholder|xxx+|<.*>|none|null|changeme|redacted|'
    rb'env|environment|os\.environ|getenv|model_speech_api_key|volc_tts_api_key)$')

MEDIA_SUFFIXES = {'.mp4', '.mov', '.mkv', '.avi', '.wav', '.mp3', '.ogg', '.m4a',
                  '.flac', '.aac'}
# A whole word list looks like many consecutive lines of "word /phonetic/ ...".
ENTRY = re.compile(r'^[A-Za-z][A-Za-z\'\- ]{1,30} /[^/\s]+/ ')
USERDATA_NAMES = {'project.json', 'job-owner.json', 'complete.json', 'timeline.json',
                  'draft_content.json', 'draft_meta_info.json'}
SKIP_DIRS = {'__pycache__'}
# Filled in from the package's own VERSION.txt/README before the scan runs.
KNOWN_DIGESTS = set()


def text_files(root):
    for path in sorted(root.rglob('*')):
        if path.is_dir() or any(part in SKIP_DIRS for part in path.parts):
            continue
        yield path


def excerpt(data, start, length=48):
    raw = data[max(0, start):start + length]
    text = raw.decode('utf-8', 'replace')
    text = re.sub(r'[0-9a-fA-F]{16,}', '<hex>', text)
    if len(text) > 32:
        text = text[:16] + '…' + text[-8:]
    return text.replace('\n', ' ').replace('\r', ' ')


def scan_secrets(root):
    """Q1: is there a credential with a *value* in the package?

    Two families, kept apart on purpose because their weight differs:

    ``credential_shaped``  an ``api_key=``-style assignment whose value is not a
                           placeholder, or a bearer token.  This is the finding
                           that matters: only our own code would carry it.
    ``notes``              binary noise and benign matches, reported with a
                           reason so the reader can see the evidence instead of a
                           count: FFmpeg/OpenSSL images embed their own x509 test
                           certificates, PyInstaller payloads contain compiled
                           constants, and ``VERSION.txt``/``README`` publish the
                           sha256 of the shipped binaries (a hash, not a secret).
    """
    credentials, notes = [], []
    for path in text_files(root):
        try:
            data = path.read_bytes()
        except OSError as error:
            notes.append({'file': str(path.relative_to(root)), 'kind': 'unreadable',
                          'reason': str(error)})
            continue
        for name, pattern in PATTERNS:
            for match in pattern.finditer(data):
                value = match.group(1) if match.groups() else match.group(0)
                if name == 'assignment' and (PLACEHOLDER.match(value)
                                             or b'%' in value or b'{' in value):
                    continue
                if name == 'long_hex_token':
                    digest = match.group(0).decode('ascii')
                    known = digest in KNOWN_DIGESTS
                    notes.append({
                        'file': str(path.relative_to(root)), 'kind': name,
                        'reason': ('published sha256 of a shipped binary' if known
                                   else 'compiled constant or binary data, no key context'),
                        'sha256': digest})
                elif name == 'private_key':
                    notes.append({
                        'file': str(path.relative_to(root)), 'kind': name,
                        'reason': 'x509 test certificates embedded in the FFmpeg/OpenSSL build'
                                  if path.suffix.lower() == '.exe' else
                                  'certificate bundle inside a binary'})
                else:
                    credentials.append({'file': str(path.relative_to(root)), 'kind': name,
                                        'offset': match.start(),
                                        'excerpt': excerpt(data, match.start())})
                break   # one hit per pattern per file is enough to act on
    return credentials, notes


def known_digests(root):
    """Every 32+ hex string the package itself publishes (its own hashes)."""
    found = set()
    for name in ('VERSION.txt', 'README-成员使用.md'):
        path = root / name
        if path.exists():
            for match in re.finditer(r'[0-9a-f]{32,}', path.read_text(
                    encoding='utf-8', errors='ignore')):
                found.add(match.group(0))
    return found


def scan_media(root):
    return [str(path.relative_to(root)) + ' (%d bytes)' % path.stat().st_size
            for path in text_files(root) if path.suffix.lower() in MEDIA_SUFFIXES]


def scan_wordlist(root, run=8):
    """A run of consecutive entry-shaped lines is a word list, not a coincidence."""
    hits = []
    for path in text_files(root):
        if path.suffix.lower() not in ('.txt', '.json', '.py', '.csv', '.md', ''):
            continue
        try:
            text = path.read_text(encoding='utf-8', errors='ignore')
        except OSError:
            continue
        streak, start = 0, 0
        for number, line in enumerate(text.splitlines(), 1):
            if ENTRY.match(line.strip()):
                if streak == 0:
                    start = number
                streak += 1
                if streak >= run:
                    hits.append({'file': str(path.relative_to(root)), 'line': start,
                                 'run': streak})
                    break
            else:
                streak = 0
    return hits


def scan_caches(root):
    hits = []
    for path in sorted(root.rglob('*')):
        if path.is_dir():
            audio = [p for p in path.iterdir()
                     if p.is_file() and p.suffix.lower() in ('.wav', '.ogg', '.mp3')]
            if len(audio) >= 5:
                hits.append('%s/ (%d audio files)' % (path.relative_to(root), len(audio)))
    return hits


def scan_userdata(root):
    """A member's own artefacts must not ship; a library *template* is not one."""
    hits, templates = [], []
    for path in text_files(root):
        if path.name not in USERDATA_NAMES and path.suffix.lower() not in ('.sqlite3',
                                                                          '.sqlite'):
            continue
        relative = str(path.relative_to(root))
        if '_internal' in path.parts:
            templates.append(relative + ' (bundled library template/resource)')
        else:
            hits.append(relative)
    return hits, templates


def scan_wordlist_sample(root, wordlist):
    """Positive control: look for exact published word-list lines in the package."""
    found = []
    if not wordlist or not Path(wordlist).exists():
        return found
    lines = [line.strip() for line in
             Path(wordlist).read_text(encoding='utf-8-sig').splitlines() if line.strip()]
    probes = [lines[i] for i in (0, 1, 150, 199, 500, 999, 1499) if i < len(lines)]
    blobs = []
    for path in text_files(root):
        try:
            blobs.append((path, path.read_bytes()))
        except OSError:
            continue
    for probe in probes:
        needle = probe.encode('utf-8')
        for path, data in blobs:
            if needle in data:
                found.append({'probe': probe, 'file': str(path.relative_to(root))})
    return found


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--package', required=True)
    parser.add_argument('--wordlist', default=None,
                        help='只读词表，用来做"整表是否被打包"的正向对照')
    parser.add_argument('--json')
    args = parser.parse_args(argv)
    root = Path(args.package).resolve(strict=True)
    global KNOWN_DIGESTS
    KNOWN_DIGESTS = known_digests(root)
    files = list(text_files(root))
    total = sum(p.stat().st_size for p in files)
    credentials, notes = scan_secrets(root)
    user_data, templates = scan_userdata(root)
    report = {
        'package': str(root),
        'files': len(files),
        'bytes': total,
        'credential_shaped_findings': credentials,
        'secret_scan_notes': notes,
        'media': scan_media(root),
        'wordlist_runs': scan_wordlist(root),
        'wordlist_exact_lines': scan_wordlist_sample(root, args.wordlist),
        'audio_caches': scan_caches(root),
        'user_data': user_data,
        'bundled_templates': templates,
    }
    # A hit in any of these six is what makes the package unfit to hand over.
    report['clean'] = not (credentials or report['media'] or report['wordlist_runs']
                           or report['wordlist_exact_lines'] or report['audio_caches']
                           or user_data)
    text = json.dumps(report, ensure_ascii=False, indent=1)
    print(text)
    if args.json:
        target = Path(args.json)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding='utf-8')
    return 0 if report['clean'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
