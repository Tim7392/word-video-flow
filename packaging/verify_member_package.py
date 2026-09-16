"""Acceptance run of a built member package, in an environment that has no Python.

This is the W09 "cleaned environment" check, kept as a script so it can be re-run
for every later package (W10 update/rollback included) instead of being retyped:

  1. ``doctor`` with PATH reduced to ``C:\\Windows\\System32`` (no Python, no FFmpeg,
     no development venv), ``PYTHONHOME``/``PYTHONPATH``/``PYTHONSTARTUP`` absent,
     working directory = the package folder.
  2. The real three-word offline job from ``data/fixtures/p1-无片头.json``: the
     fixture is copied, ``output`` and ``source.path`` are pointed at a scratch
     folder, and the job is submitted and started through the packaged launcher.
     The engine starts its worker with ``sys.executable``, so a completed job is
     also the proof that the packaged worker process starts and holds the job lock.
  3. Both steps run with a fresh ``--db`` inside the scratch folder, and the
     packaged ffprobe re-reads the produced MP4.

Usage (locked interpreter; it only *drives* the package, it never runs the app):

    & <work root>\\runtime\\venv\\Scripts\\python.exe packaging\\verify_member_package.py `
        --package <work root>\\out\\packages\\word-video-member-0.1.0

Evidence JSON goes to stdout; progress goes to stderr.
"""
import argparse
import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

LAUNCHER_NAME = '单词视频.cmd'
APP_DIR = 'WordVideo'
FFMPEG_DIR = 'ffmpeg'
FFPROBE = '%s/%s' % (FFMPEG_DIR, 'ffprobe.exe')
FIXTURE_NAME = 'p1-无片头.json'
SRT_TRACKS = ('_01_英文重复.srt', '_02_英文单次.srt', '_03_音标.srt',
              '_04_中文带词性.srt', '_05_中文无词性.srt')


def repo_root():
    return Path(__file__).resolve().parents[1]


def _load_builder():
    """Reuse the builder's work-root rule instead of keeping a second copy of it."""
    path = Path(__file__).resolve().parent / 'build_member_package.py'
    spec = importlib.util.spec_from_file_location('packaging_build_member_package', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


builder = _load_builder()


def work_root():
    return builder.find_work_root(repo_root()) or repo_root().parent


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def clean_environment(scratch, system_root=r'C:\Windows'):
    """PATH holds only System32; no Python variable, no venv, no development tools."""
    system32 = '%s\\System32' % system_root
    keep = ('USERPROFILE', 'APPDATA', 'LOCALAPPDATA', 'HOMEDRIVE', 'HOMEPATH', 'PROGRAMDATA',
            'PUBLIC', 'PROCESSOR_ARCHITECTURE', 'PROCESSOR_IDENTIFIER', 'NUMBER_OF_PROCESSORS')
    environment = {name: os.environ[name] for name in keep if os.environ.get(name)}
    environment.update({
        'SystemRoot': system_root, 'windir': system_root, 'SystemDrive': str(Path(system_root).drive),
        'ComSpec': '%s\\cmd.exe' % system32, 'PATH': system32,
        'PATHEXT': '.COM;.EXE;.BAT;.CMD',
        'TEMP': str(scratch), 'TMP': str(scratch)})
    return environment


def run_launcher(package, arguments, environment, timeout=300):
    """One CLI call through the packaged launcher; stdout must be exactly one JSON."""
    package = Path(package)
    command = [environment['ComSpec'], '/c', LAUNCHER_NAME] + list(arguments)
    started = time.monotonic()
    try:
        result = subprocess.run(command, cwd=str(package), env=environment, capture_output=True,
                                text=True, encoding='utf-8', errors='replace', timeout=timeout)
    except subprocess.TimeoutExpired as error:
        return {'command': ' '.join([LAUNCHER_NAME] + list(arguments)), 'timeout': True,
                'returncode': None, 'seconds': round(time.monotonic() - started, 2),
                'stdout_lines': 0, 'stdout': (error.stdout or '')[-2000:],
                'stderr': 'TIMEOUT after %ss' % timeout}
    seconds = round(time.monotonic() - started, 2)
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    record = {'command': ' '.join(['cd /d %s &&' % package, LAUNCHER_NAME] + list(arguments)),
              'returncode': result.returncode, 'seconds': seconds,
              'stdout_lines': len(lines), 'stderr': result.stderr.strip()[-2000:],
              'stdout': result.stdout.strip()[:4000]}
    if len(lines) == 1:
        try:
            record['json'] = json.loads(lines[0])
        except ValueError as error:
            record['parse_error'] = str(error)
    else:
        record['parse_error'] = 'expected exactly one JSON line, got %d' % len(lines)
    return record


def prepare_request(fixture, workspace, output_root):
    """Copy the three-word fixture, pointing it at scratch paths only."""
    request = json.loads(Path(fixture).read_text(encoding='utf-8-sig'))
    source = Path(request['source']['path'])
    local = work_root() / 'data' / 'wordlists' / source.name
    origin = local if local.is_file() else source
    wordlist = Path(workspace) / 'wordlist.txt'
    shutil.copy2(origin, wordlist)
    request['source']['path'] = str(wordlist)
    request['output'] = str(output_root)
    request_path = Path(workspace) / 'request.json'
    request_path.write_text(json.dumps(request, ensure_ascii=False, indent=1), encoding='utf-8')
    return {'path': str(request_path), 'wordlist': str(wordlist),
            'wordlist_sha256': sha256_file(wordlist),
            'wordlist_origin': str(origin), 'wordlist_origin_inside_work_root': origin != source,
            'output': str(output_root), 'modified_fields': ['source.path', 'output'],
            'fixture': str(fixture), 'fixture_sha256': sha256_file(fixture)}


def prepare_docx_request(fixture, workspace, output_root):
    """A two-entry .docx source: reading it proves python-docx works in the package."""
    import docx  # development-side only; the packaged app does its own parsing
    document = docx.Document()
    document.add_paragraph('alpha /ˈælfə/ n. 阿尔法')
    document.add_paragraph('beta /ˈbiːtə/ n. 贝塔')
    path = Path(workspace) / 'wordlist.docx'
    document.save(str(path))

    request = json.loads(Path(fixture).read_text(encoding='utf-8-sig'))
    request['idempotency_key'] = 'docx-source-check'
    request['source'] = {'path': str(path)}
    # Selecting entry 2 only succeeds when the document really yielded two entries.
    request['range'] = {'start': 2, 'end': 2}
    request['output'] = str(output_root / 'docx-check')
    request['provider']['items'] = request['provider']['items'][:1]
    request_path = Path(workspace) / 'request-docx.json'
    request_path.write_text(json.dumps(request, ensure_ascii=False, indent=1), encoding='utf-8')
    return {'path': str(request_path), 'document': str(path),
            'document_sha256': sha256_file(path),
            'expects': 'submit accepts range 2..2 read from the .docx'}


def poll_job(package, db, job, environment, timeout):
    """Wait for the worker to finish; the state transitions can only come from it."""
    observed = []
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        last = run_launcher(package, ['--db', db, 'status', '--job', job], environment, timeout=120)
        state = ((last.get('json') or {}).get('result') or {}).get('state')
        if state not in observed:
            observed.append(state)
            print('[verify] job state: %s' % state, file=sys.stderr, flush=True)
        if state in ('generated', 'failed', 'interrupted', 'cancelled', 'paused'):
            return {'final': last, 'states_observed': observed}
        time.sleep(2)
    return {'final': last, 'states_observed': observed, 'timeout_seconds': timeout}


def batch_manifests(root):
    """Batch ``complete.json`` files.

    The audio cache keeps one ``complete.json`` marker per cached utterance (with an
    empty file list), so a plain rglob would count those as batches; a batch manifest
    is the one that carries the ``batch`` block written by ``jobs.work``.
    """
    manifests = []
    for path in sorted(Path(root).rglob('complete.json')):
        if 'audio-cache' in path.relative_to(root).parts:
            continue
        try:
            saved = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            continue
        if 'batch' in saved:
            manifests.append((path, saved))
    return manifests


def check_artifacts(package, output_root, job, job_result):
    """Three products, the file manifest, and a re-read of the MP4 by the packaged ffprobe."""
    root = Path(output_root) / job
    report = {'job_directory': str(root), 'batch_manifests': [], 'problems': []}
    manifests = batch_manifests(root)
    report['batch_count'] = len(manifests)
    if not manifests:
        report['problems'].append('no batch complete.json under %s' % root)
        return report
    # The CLI's stdout contract deliberately reports only SRT/MP4/draft files, while
    # complete.json is the full manifest, so the two are compared one way only.
    reported = [item['path'] for item in job_result.get('files', [])]
    report['reported_files'] = len(reported)
    report['reported_file_count'] = job_result.get('file_count')
    for marker, saved in manifests:
        listed = saved.get('files', [])
        records = {item['path']: item['sha256'] for item in listed}
        report['batch_manifests'].append({
            'complete': str(marker), 'files': len(listed),
            'video_check': saved.get('batch', {}).get('video_check') is not None,
            'batch': saved.get('batch', {}).get('first')})
        if not listed:
            report['problems'].append('%s lists no files' % marker)
        for path, digest in records.items():
            if not Path(path).is_file():
                report['problems'].append('missing %s' % path)
            elif sha256_file(path) != digest:
                report['problems'].append('changed %s' % path)
        absent = [path for path in reported if path not in records]
        if absent:
            report['problems'].append('reported but not in the manifest: %s' % ', '.join(absent))
        if job_result.get('file_count') != len(records):
            report['problems'].append('CLI file_count=%s but the manifest lists %d'
                                      % (job_result.get('file_count'), len(records)))
        folder = marker.parent
        srts = sorted((folder / 'srt').glob('*.srt'))
        report['srt_files'] = [item.name for item in srts]
        if len(srts) != 5:
            report['problems'].append('expected 5 SRT files, found %d' % len(srts))
        missing_tracks = [name for name in SRT_TRACKS
                          if not any(item.name.endswith(name) for item in srts)]
        if missing_tracks:
            report['problems'].append('missing SRT tracks: %s' % ', '.join(missing_tracks))
        video = folder / 'video' / 'video.mp4'
        report['video'] = str(video)
        if not video.is_file():
            report['problems'].append('missing %s' % video)
        else:
            probe = subprocess.run([str(Path(package) / FFPROBE), '-v', 'error', '-show_streams',
                                    '-show_format', '-of', 'json', str(video)],
                                   capture_output=True, text=True, encoding='utf-8',
                                   errors='replace', timeout=120)
            if probe.returncode:
                report['problems'].append('packaged ffprobe failed on the MP4')
            else:
                info = json.loads(probe.stdout)
                kinds = [stream.get('codec_type') for stream in info.get('streams', [])]
                video_stream = next((stream for stream in info.get('streams', [])
                                     if stream.get('codec_type') == 'video'), {})
                report['video_probe'] = {'duration': info.get('format', {}).get('duration'),
                                         'streams': kinds,
                                         'size': '%sx%s' % (video_stream.get('width'),
                                                            video_stream.get('height')),
                                         'codec': video_stream.get('codec_name')}
                if kinds.count('video') != 1 or kinds.count('audio') != 1:
                    report['problems'].append('unexpected MP4 streams: %s' % kinds)
        draft = folder / 'editable-draft'
        resources = list((draft / 'Resources').glob('*')) if (draft / 'Resources').is_dir() else []
        report['draft'] = {'draft_content': (draft / 'draft_content.json').is_file(),
                           'draft_meta': (draft / 'draft_meta_info.json').is_file(),
                           'resources': len(resources), 'size_bytes': sum(
                               item.stat().st_size for item in draft.rglob('*') if item.is_file())}
        if not (report['draft']['draft_content'] and report['draft']['draft_meta']):
            report['problems'].append('editable draft is incomplete')
    report['ok'] = not report['problems']
    report['problems'] = report['problems'][:20]
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--package', required=True, help='built package folder')
    parser.add_argument('--tmp', help='scratch root (default: <work root>/runtime/tmp/C/verify)')
    parser.add_argument('--timeout', type=int, default=900, help='seconds to wait for the job')
    parser.add_argument('--report', help='also write the evidence JSON here')
    parser.add_argument('--skip-job', action='store_true', help='run doctor only')
    parser.add_argument('--skip-docx', action='store_true', help='skip the .docx source check')
    args = parser.parse_args(argv)
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')

    package = Path(args.package).resolve()
    launcher = package / LAUNCHER_NAME
    if not launcher.is_file():
        raise SystemExit('no launcher at %s' % launcher)
    root = work_root()
    tmp_root = Path(args.tmp).resolve() if args.tmp else root / 'runtime' / 'tmp' / 'C' / 'verify'
    scratch = tmp_root / datetime.now().strftime('%Y%m%d-%H%M%S')
    for stale in sorted(tmp_root.glob('*')) if tmp_root.is_dir() else []:
        if stale.is_dir() and stale != scratch:
            shutil.rmtree(stale, ignore_errors=True)
    (scratch / 'temp').mkdir(parents=True)
    environment = clean_environment(scratch / 'temp')
    evidence = {'package': str(package), 'started': datetime.now().astimezone().strftime(
                    '%Y-%m-%d %H:%M:%S %z'), 'scratch': str(scratch),
                'clean_environment': {name: environment[name]
                                      for name in ('PATH', 'TEMP', 'TMP', 'SystemRoot')},
                'python_variables_present': [name for name in
                                             ('PYTHONHOME', 'PYTHONPATH', 'PYTHONSTARTUP')
                                             if name in environment],
                'package_file_count': sum(1 for item in package.rglob('*') if item.is_file()),
                'package_bytes': sum(item.stat().st_size for item in package.rglob('*')
                                     if item.is_file())}

    print('[verify] doctor in a PATH=%s environment' % environment['PATH'], file=sys.stderr)
    doctor = run_launcher(package, ['doctor'], environment, timeout=180)
    result = (doctor.get('json') or {}).get('result') or {}
    fonts = result.get('fonts', {})
    evidence['doctor'] = {
        'one_json_line': doctor.get('stdout_lines') == 1 and 'json' in doctor,
        'returncode': doctor['returncode'], 'seconds': doctor['seconds'],
        'ok': bool((doctor.get('json') or {}).get('ok')),
        'ffmpeg': result.get('ffmpeg'), 'ffprobe': result.get('ffprobe'),
        'font_substitutions': result.get('font_substitutions'),
        'font_missing': result.get('font_missing'),
        'font_sources': {role: value.get('source') for role, value in fonts.items()},
        'ui_modules_loaded': result.get('ui_modules_loaded'),
        'tts_key_configured': result.get('tts_key_configured'),
        'stderr': doctor['stderr']}
    evidence['doctor']['ffmpeg_inside_package'] = str(package) in str(result.get('ffmpeg') or '')
    evidence['doctor']['ok'] = bool(
        evidence['doctor']['ok'] and evidence['doctor']['one_json_line']
        and evidence['doctor']['ffmpeg_inside_package']
        and not result.get('font_substitutions') and not result.get('font_missing')
        and result.get('ui_modules_loaded') is False)
    print('[verify] doctor ok=%s in %.2fs' % (evidence['doctor']['ok'], doctor['seconds']),
          file=sys.stderr)

    if not args.skip_job:
        fixture = root / 'data' / 'fixtures' / FIXTURE_NAME
        output_root = scratch / 'out'
        request = prepare_request(fixture, scratch, output_root)
        evidence['request'] = request
        db = str(scratch / 'jobs.sqlite3')
        submit = run_launcher(package, ['--db', db, 'submit', '--request', request['path']],
                              environment, timeout=300)
        evidence['submit'] = {key: submit[key] for key in
                              ('command', 'returncode', 'seconds', 'stdout_lines', 'stderr')}
        evidence['submit']['ok'] = bool((submit.get('json') or {}).get('ok'))
        evidence['submit']['one_json_line'] = submit.get('stdout_lines') == 1 and 'json' in submit
        evidence['submit']['json'] = submit.get('json')
        job = ((submit.get('json') or {}).get('result') or {}).get('id')
        evidence['job'] = job
        print('[verify] submit ok=%s job=%s' % (evidence['submit']['ok'], job), file=sys.stderr)
        if not job:
            evidence['ok'] = False
            print(json.dumps(evidence, ensure_ascii=False, indent=1))
            return 1
        started = run_launcher(package, ['--db', db, 'start', '--job', job], environment, timeout=300)
        worker = ((started.get('json') or {}).get('result') or {})
        evidence['start'] = started.get('json')
        evidence['start_command'] = started['command']
        evidence['start_seconds'] = started['seconds']
        evidence['worker'] = {'pid': worker.get('pid'), 'log': worker.get('log'),
                              'separate_process': bool(worker.get('pid')) and worker.get('pid') != os.getpid(),
                              'artifacts_before_worker': (output_root / job / 'complete.json').exists()}
        print('[verify] start returned in %.2fs, worker pid=%s'
              % (started['seconds'], worker.get('pid')), file=sys.stderr)
        job_started = time.monotonic()
        poll = poll_job(package, db, job, environment, args.timeout)
        evidence['job_seconds'] = round(time.monotonic() - job_started, 1)
        final = (poll['final'].get('json') or {}).get('result') or {}
        evidence['poll'] = {'states_observed': poll['states_observed'],
                            'final_state': final.get('state'), 'seconds': poll['final']['seconds'],
                            'error': final.get('error')}
        evidence['worker_log'] = None
        log = Path(db).parent / (job + '.worker.log')
        if log.is_file():
            evidence['worker_log'] = {'path': str(log), 'bytes': log.stat().st_size,
                                      'tail': log.read_text(encoding='utf-8', errors='replace')[-800:]}
        evidence['job_result'] = final.get('result')
        evidence['artifacts'] = check_artifacts(package, output_root, job, final.get('result') or {})
        evidence['external_temp_entries'] = [item.name for item in (scratch / 'temp').glob('*')]
        evidence['package_temp_entries'] = [item.name for item in (package / 'temp').glob('*')] \
            if (package / 'temp').is_dir() else None
        evidence['stage_seconds'] = (final.get('result') or {}).get('stage_seconds')

        if not args.skip_docx:
            docx_request = prepare_docx_request(fixture, scratch, output_root)
            evidence['docx_request'] = docx_request
            docx_submit = run_launcher(package, ['--db', db, 'submit', '--request',
                                                 docx_request['path']], environment, timeout=300)
            evidence['docx_submit'] = {'command': docx_submit['command'],
                                       'returncode': docx_submit['returncode'],
                                       'ok': bool((docx_submit.get('json') or {}).get('ok')),
                                       'error': ((docx_submit.get('json') or {}).get('error')),
                                       'id': ((docx_submit.get('json') or {}).get('result') or {}).get('id')}
            print('[verify] .docx source submit ok=%s' % evidence['docx_submit']['ok'],
                  file=sys.stderr)

    checks = {'doctor_ok': evidence['doctor']['ok']}
    if 'artifacts' in evidence:
        checks.update({
            'submit_ok': evidence['submit']['ok'],
            'worker_is_separate_process': evidence['worker']['separate_process'],
            'no_artifacts_before_worker': evidence['worker']['artifacts_before_worker'] is False,
            'job_state_generated': evidence['poll']['final_state'] == 'generated',
            'worker_log_written': bool(evidence['worker_log']),
            'artifacts_ok': evidence['artifacts']['ok'],
            'package_temp_created': evidence['package_temp_entries'] is not None,
            'no_external_temp_pollution': not evidence['external_temp_entries']})
    if 'docx_submit' in evidence:
        checks['docx_source_ok'] = evidence['docx_submit']['ok']
    evidence['checks'] = checks
    evidence['ok'] = all(checks.values())
    evidence['finished'] = datetime.now().astimezone().strftime('%Y-%m-%d %H:%M:%S %z')
    payload = json.dumps(evidence, ensure_ascii=False, indent=1)
    if args.report:
        Path(args.report).write_text(payload, encoding='utf-8')
        print('[verify] evidence written to %s' % args.report, file=sys.stderr)
    print(payload)
    return 0 if evidence['ok'] else 1


if __name__ == '__main__':
    sys.exit(main())
