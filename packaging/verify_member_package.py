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
  3. **The editor**, which is what a member actually opens: the launcher with no
     arguments must start ``WordVideoEditor.exe`` (the windowed build) and leave it
     running, and the same launcher driven with ``--editor --import ... --export
     --report ...`` must deliver the three products *through the window*.  The
     published folder is then handed to the independent checker
     ``tests/acceptance/accept_range.py --cleaning v2 --pixels all``, because the
     editor approving its own output is not evidence.
  4. Both steps run with a fresh ``--db`` inside the scratch folder, and the
     packaged ffprobe re-reads the produced MP4.

Usage (locked interpreter; it only *drives* the package, it never runs the app):

    & <work root>\\runtime\\venv\\Scripts\\python.exe packaging\\verify_member_package.py `
        --package <work root>\\out\\packages\\word-video-member-0.3.0

Evidence JSON goes to stdout; progress goes to stderr.
"""
import argparse
import csv
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
EDITOR_DIR = 'WordVideoEditor'
EDITOR_EXE = 'WordVideoEditor.exe'
FFMPEG_DIR = 'ffmpeg'
FFPROBE = '%s/%s' % (FFMPEG_DIR, 'ffprobe.exe')
FIXTURE_NAME = 'p1-无片头.json'
GUI_FIXTURE_NAME = 'p1-有片头.json'
GUI_WORDS = (151, 153)
CHECKER = 'tests/acceptance/accept_range.py'
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
    """PATH holds only System32; no Python variable, no venv, no development tools.

    ``SystemRoot``/``windir``/``SystemDrive``/``ComSpec`` are set **explicitly**
    rather than left to inheritance.  A restricted runner (QA's, or a hardened CI
    image) may start this script without them, and a packaged exe that cannot see
    ``SystemRoot`` exits 1 with completely empty stderr - which looks exactly like
    a broken package and costs an hour to tell apart.  Which names the harness
    supplied, and which of those the parent process did not have, is reported in
    the evidence as ``environment_provenance`` instead of being assumed.
    """
    system32 = '%s\\System32' % system_root
    keep = ('USERPROFILE', 'APPDATA', 'LOCALAPPDATA', 'HOMEDRIVE', 'HOMEPATH', 'PROGRAMDATA',
            'PUBLIC', 'PROCESSOR_ARCHITECTURE', 'PROCESSOR_IDENTIFIER', 'NUMBER_OF_PROCESSORS')
    environment = {name: os.environ[name] for name in keep if os.environ.get(name)}
    supplied = {
        'SystemRoot': system_root, 'windir': system_root,
        'SystemDrive': str(Path(system_root).drive),
        'ComSpec': '%s\\cmd.exe' % system32, 'PATH': system32,
        'PATHEXT': '.COM;.EXE;.BAT;.CMD',
        'TEMP': str(scratch), 'TMP': str(scratch)}
    added = sorted(name for name in supplied if not os.environ.get(name))
    environment.update(supplied)
    #: What this harness decided, as opposed to what it merely passed through.
    environment_provenance = {
        'inherited': sorted(name for name in keep if name in environment),
        'set_by_harness': sorted(supplied),
        'added_by_harness': added,
        'note': 'added_by_harness lists variables the parent process did not have and '
                'the harness supplied; a container or restricted runner missing '
                'SystemRoot/windir is what this prevents'}
    return environment, environment_provenance


def run_command(package, arguments, environment, timeout=300):
    """One call through the packaged launcher; raw result, no contract assumed.

    The CLI route's contract (exactly one JSON line on stdout) is checked by
    :func:`run_launcher`, which builds on this.  The editor's route deliberately
    writes nothing to stdout - a windowed build has no console - so it needs the
    same driver without the stdout rule.
    """
    package = Path(package)
    command = [environment['ComSpec'], '/c', LAUNCHER_NAME] + [str(item) for item in arguments]
    started = time.monotonic()
    try:
        result = subprocess.run(command, cwd=str(package), env=environment, capture_output=True,
                                text=True, encoding='utf-8', errors='replace', timeout=timeout)
    except subprocess.TimeoutExpired as error:
        return {'command': ' '.join([LAUNCHER_NAME] + [str(i) for i in arguments]),
                'timeout': True, 'returncode': None,
                'seconds': round(time.monotonic() - started, 2), 'stdout_lines': 0,
                'stdout': (error.stdout or '')[-2000:],
                'stderr': 'TIMEOUT after %ss' % timeout}
    seconds = round(time.monotonic() - started, 2)
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    return {'command': ' '.join(['cd /d %s &&' % package, LAUNCHER_NAME]
                                + [str(i) for i in arguments]),
            'returncode': result.returncode, 'seconds': seconds,
            'stdout_lines': len(lines), 'stderr': result.stderr.strip()[-2000:],
            'stdout': result.stdout.strip()[:4000]}


def run_launcher(package, arguments, environment, timeout=300):
    """One CLI call through the packaged launcher; stdout must be exactly one JSON."""
    record = run_command(package, arguments, environment, timeout)
    lines = [line for line in record['stdout'].splitlines() if line.strip()]
    if record.get('timeout'):
        return record
    if len(lines) == 1:
        try:
            record['json'] = json.loads(lines[0])
        except ValueError as error:
            record['parse_error'] = str(error)
    else:
        record['parse_error'] = 'expected exactly one JSON line, got %d' % len(lines)
    return record


def start_detached(package, environment, timeout=120):
    """Run the launcher with no arguments - the member's double-click - without pipes.

    Deliberately ``DEVNULL`` rather than ``capture_output``: the launcher starts the
    window and returns immediately, and the process it starts inherits this caller's
    handles.  Measured: with pipes, a run of the launcher held the pipe open until
    the editor was closed, so the caller sat there for its whole timeout and then
    reported "the editor never started" about an editor that was running.
    """
    package = Path(package)
    command = [environment['ComSpec'], '/c', LAUNCHER_NAME]
    started = time.monotonic()
    record = {'command': ' '.join(['cd /d %s &&' % package, LAUNCHER_NAME]),
              'capture': 'devnull (the window would inherit a pipe and hold it open)',
              'stdout': '', 'stderr': '', 'stdout_lines': 0}
    try:
        result = subprocess.run(command, cwd=str(package), env=environment,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                timeout=timeout)
    except subprocess.TimeoutExpired:
        record.update({'timeout': True, 'returncode': None,
                       'seconds': round(time.monotonic() - started, 2)})
        return record
    record.update({'timeout': False, 'returncode': result.returncode,
                   'seconds': round(time.monotonic() - started, 2)})
    return record


def process_ids(image, environment):
    """PIDs of a running image, by asking Windows instead of trusting our own spawn.

    A windowed executable gives its parent nothing to hold on to (the launcher
    ``start``s it and returns), so "the editor is running" has to be an observation
    about the machine.  ``tasklist`` is in System32, which is the one directory the
    cleaned environment keeps on PATH.
    """
    result = subprocess.run([environment['ComSpec'], '/c', 'tasklist', '/FI',
                             'IMAGENAME eq %s' % image, '/FO', 'CSV', '/NH'],
                            capture_output=True, text=True, encoding='utf-8',
                            errors='replace', timeout=120)
    pids = []
    for line in (result.stdout or '').splitlines():
        if not line.startswith('"'):
            continue
        fields = next(csv.reader([line]), [])
        if fields and fields[0].lower() == image.lower():
            pids.append(fields[1])
    return {'pids': pids, 'returncode': result.returncode,
            'stdout': (result.stdout or '').strip()[:400]}


def kill_image(image, environment):
    result = subprocess.run([environment['ComSpec'], '/c', 'taskkill', '/IM', image, '/F'],
                            capture_output=True, text=True, encoding='utf-8',
                            errors='replace', timeout=120)
    return {'returncode': result.returncode, 'stdout': (result.stdout or '').strip()[:300]}


def checker_path():
    return repo_root() / CHECKER


def run_checker(run_dir, wordlist, words, out_json, timeout=3600):
    """The independent checker, quoted verbatim: the editor does not grade itself."""
    checker = checker_path()
    if not checker.is_file():
        return {'ran': False, 'reason': 'checker not present at %s' % checker}
    command = [sys.executable, str(checker), str(run_dir), '--source', str(wordlist),
               '--range', '%d-%d' % (words[0], words[-1]), '--cleaning', 'v2',
               '--pixels', 'all', '--json', str(out_json)]
    started = time.monotonic()
    result = subprocess.run(command, capture_output=True, text=True, encoding='utf-8',
                            errors='replace', timeout=timeout)
    payload = {'ran': True, 'command': ' '.join(str(item) for item in command),
               'returncode': result.returncode,
               'seconds': round(time.monotonic() - started, 2),
               'stdout_tail': (result.stdout or '').strip().splitlines()[-12:],
               'stderr_tail': (result.stderr or '').strip().splitlines()[-6:]}
    if Path(out_json).is_file():
        report = json.loads(Path(out_json).read_text(encoding='utf-8'))
        payload['verdict'] = report.get('verdict')
        payload['failures'] = report.get('failures')
        payload['face_summaries'] = {
            name: ({key: value[key] for key in ('problem_count', 'problems')
                    if key in value} if isinstance(value, dict) else value)
            for name, value in report.items()
            if name in ('integrity', 'timing', 'srt', 'draft', 'pixels', 'mix', 'video')}
    return payload


def gui_fixture(root):
    """The three-word fixture that also carries the intro layer, and its word list."""
    path = Path(root) / 'data' / 'fixtures' / GUI_FIXTURE_NAME
    if not path.is_file():
        return None
    return path


def gui_smoke(package, environment, scratch, root, timeout, words=GUI_WORDS):
    """Open the editor the way a member does, then deliver through the window.

    Two runs, because they answer two different questions:

    * **no arguments at all** - the member's double-click.  The launcher must leave
      ``WordVideoEditor.exe`` running (observed by ``tasklist``, not by asking the
      launcher how it went), and that run is stopped again so it cannot be mistaken
      for the next one;
    * **``--editor --import ... --export --report ...``** - the same window, driven,
      ending in a published folder that the independent checker judges.  Its report
      carries the two numbers a member feels: how long until a window appears, and
      how long the delivery takes.
    """
    package = Path(package)
    evidence = {'package_editor_dir': str(package / EDITOR_DIR),
                'editor_exe_present': (package / EDITOR_DIR / EDITOR_EXE).is_file()}
    if not evidence['editor_exe_present']:
        evidence['ok'] = False
        evidence['reason'] = 'the package has no %s/%s' % (EDITOR_DIR, EDITOR_EXE)
        return evidence

    # Nothing of this name may be running before the smoke, or "it started" could be
    # somebody else's process - including a previous, failed run of this script.
    before = process_ids(EDITOR_EXE, environment)
    evidence['processes_before'] = before
    if before['pids']:
        evidence['pre_existing_killed'] = kill_image(EDITOR_EXE, environment)
        time.sleep(1.0)

    print('[verify] launcher with no arguments (the member double-click)', file=sys.stderr)
    launched = start_detached(package, environment, timeout=120)
    deadline = time.monotonic() + 60
    observed = {'pids': []}
    while time.monotonic() < deadline:
        observed = process_ids(EDITOR_EXE, environment)
        if observed['pids']:
            break
        time.sleep(1.0)
    time.sleep(3.0)
    survived = process_ids(EDITOR_EXE, environment)
    evidence['default_launch'] = {
        'launcher': {key: launched[key] for key in ('command', 'returncode', 'seconds',
                                                    'timeout', 'capture', 'stdout', 'stderr')},
        'editor_started': bool(observed['pids']),
        'editor_pids': observed['pids'],
        'editor_still_running_after_3s': bool(survived['pids']),
        'tasklist': survived}
    evidence['default_launch']['ok'] = bool(
        launched.get('returncode') == 0 and observed['pids'] and survived['pids'])
    print('[verify] default launch: started=%s survived=%s (%.2fs)'
          % (evidence['default_launch']['editor_started'],
             evidence['default_launch']['editor_still_running_after_3s'],
             launched['seconds']), file=sys.stderr)
    evidence['killed_after_default_launch'] = kill_image(EDITOR_EXE, environment)
    time.sleep(1.0)

    fixture = gui_fixture(root)
    if fixture is None:
        evidence['ok'] = evidence['default_launch']['ok']
        evidence['reason'] = 'no %s; the scripted delivery was not run' % GUI_FIXTURE_NAME
        return evidence

    project = Path(scratch) / 'editor-project'
    output = Path(scratch) / 'editor-out'
    workspace = Path(scratch) / 'editor-work'
    workspace.mkdir(parents=True, exist_ok=True)
    request = prepare_request(fixture, workspace, output)
    evidence['request'] = request
    report_path = Path(scratch) / 'editor-run.json'
    print('[verify] editor: import + export through the launcher', file=sys.stderr)
    driven = run_command(package,
                         ['--editor', '--import', request['path'], '--into', str(project),
                          '--export', '--report', str(report_path)],
                         environment, timeout=timeout)
    evidence['driven_run'] = {key: driven[key] for key in
                              ('command', 'returncode', 'seconds', 'stdout_lines',
                               'stdout', 'stderr')}
    evidence['driven_run']['report_written'] = report_path.is_file()
    report = json.loads(report_path.read_text(encoding='utf-8')) if report_path.is_file() else {}
    evidence['editor_report'] = {
        'mode': report.get('mode'),
        'ok': report.get('ok'),
        'frozen': (report.get('environment') or {}).get('frozen'),
        'entry_to_window_seconds': report.get('entry_to_window_seconds'),
        'qt': report.get('qt'),
        'window_shown': report.get('window_shown'),
        'canvas_present': report.get('canvas_present'),
        'timeline': report.get('timeline'),
        'preview': report.get('preview'),
        'import': report.get('import'),
        'environment': report.get('environment'),
        'error': report.get('error')}
    exported = report.get('export') or {}
    evidence['delivery'] = {key: exported.get(key) for key in
                            ('ok', 'started', 'seconds_wall', 'run_dir', 'reason',
                             'messages', 'saved_revision', 'file_count')}
    evidence['delivery']['seconds_engine'] = exported.get('seconds')
    evidence['delivery']['file_count'] = len(exported.get('files') or [])
    evidence['delivery']['file_sample'] = (exported.get('files') or [])[:12]
    run_dir = exported.get('run_dir') or ''
    evidence['delivery_import_seconds'] = (report.get('import') or {}).get('seconds')
    print('[verify] editor export ok=%s in %.1fs -> %s'
          % (exported.get('ok'), driven['seconds'], run_dir or '(no run dir)'), file=sys.stderr)

    wordlist = request['wordlist']
    if run_dir:
        evidence['acceptance'] = run_checker(run_dir, wordlist, words,
                                             Path(scratch) / 'acceptance-report.json')
        print('[verify] independent checker verdict=%s failures=%s'
              % ((evidence['acceptance'] or {}).get('verdict'),
                 (evidence['acceptance'] or {}).get('failures')), file=sys.stderr)

    environment_seen = report.get('environment') or {}
    path_entries = [item for item in (environment_seen.get('PATH') or '').split(os.pathsep)
                    if item]
    package_text = str(package)
    evidence['cleaned_environment_seen_by_the_editor'] = {
        'PATH': environment_seen.get('PATH'),
        'PATH_entries_outside_package': [item for item in path_entries
                                         if not item.lower().startswith(package_text.lower())
                                         and item.lower() != 'c:\\windows\\system32'],
        'TEMP': environment_seen.get('TEMP'), 'TMP': environment_seen.get('TMP'),
        'PYTHONHOME': environment_seen.get('PYTHONHOME'),
        'PYTHONPATH': environment_seen.get('PYTHONPATH'),
        'tts_credentials_present': environment_seen.get('tts_credentials_present'),
        'cwd': environment_seen.get('cwd')}
    after = process_ids(EDITOR_EXE, environment)
    evidence['processes_after_driven_run'] = after
    if after['pids']:
        evidence['killed_after_driven_run'] = kill_image(EDITOR_EXE, environment)

    evidence['checks'] = {
        'default_launch_opens_the_editor': evidence['default_launch']['ok'],
        'driven_run_exit_code_0': driven['returncode'] == 0,
        'editor_report_written': bool(report),
        'editor_report_ok': bool(report.get('ok')),
        'window_was_shown': bool(report.get('window_shown')),
        'preview_session_opened': bool((report.get('preview') or {}).get('has_session')),
        'delivery_ok': bool(exported.get('ok')),
        'delivery_has_a_run_dir': bool(run_dir),
        'acceptance_verdict_pass': (evidence.get('acceptance') or {}).get('verdict') == 'PASS',
        'no_python_variables': not (environment_seen.get('PYTHONHOME')
                                    or environment_seen.get('PYTHONPATH')),
        'no_tts_credentials': not environment_seen.get('tts_credentials_present'),
        'path_stays_inside_the_package': not evidence[
            'cleaned_environment_seen_by_the_editor']['PATH_entries_outside_package'],
        'editor_not_left_running': not after['pids']}
    evidence['ok'] = all(evidence['checks'].values())
    return evidence


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
    parser.add_argument('--skip-gui', action='store_true',
                        help='skip the editor smoke (launcher opens it + import/export '
                             'through the window + independent checker)')
    parser.add_argument('--gui-platform', choices=('offscreen', 'windows'), default='offscreen',
                        help='QT_QPA_PLATFORM for the editor smoke; offscreen (default) keeps '
                             'the run off the machine\'s display, windows proves the real '
                             'platform plugin loads too')
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
    environment, provenance = clean_environment(scratch / 'temp')
    evidence = {'package': str(package), 'started': datetime.now().astimezone().strftime(
                    '%Y-%m-%d %H:%M:%S %z'), 'scratch': str(scratch),
                'clean_environment': {name: environment[name]
                                      for name in ('PATH', 'TEMP', 'TMP', 'SystemRoot',
                                                   'windir', 'ComSpec')},
                'environment_provenance': provenance,
                'python_variables_present': [name for name in
                                             ('PYTHONHOME', 'PYTHONPATH', 'PYTHONSTARTUP')
                                             if name in environment],
                'package_file_count': sum(1 for item in package.rglob('*') if item.is_file()),
                'package_bytes': sum(item.stat().st_size for item in package.rglob('*')
                                     if item.is_file())}
    version_file = package / 'VERSION.txt'
    evidence['built_package'] = (
        dict(line.split('=', 1) for line in
             version_file.read_text(encoding='utf-8').splitlines() if '=' in line)
        if version_file.is_file() else None)

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
    if not args.skip_gui:
        # The GUI checks are counted one by one rather than as one aggregate: an
        # aggregate would let "the window opened" hide "the delivery was refused".
        # QT_QPA_PLATFORM is added to the *GUI* environment only, and the editor's own
        # report says which plugin it actually loaded - so "it ran offscreen" is a
        # statement about the run, not about the variable this script set.
        gui_environment = dict(environment)
        gui_environment['QT_QPA_PLATFORM'] = args.gui_platform
        evidence['gui_environment'] = {'QT_QPA_PLATFORM': args.gui_platform,
                                       'PATH': environment['PATH'],
                                       'TEMP': environment['TEMP']}
        evidence['gui'] = gui_smoke(package, gui_environment, scratch, root, args.timeout)
        checks.update({'gui_%s' % name: bool(value)
                       for name, value in (evidence['gui'].get('checks') or {}).items()})
        checks['gui_smoke_ran'] = bool(evidence['gui'].get('checks'))
        checks['gui_used_the_requested_platform'] = bool(
            ((evidence['gui'].get('editor_report') or {}).get('qt') or {}).get('platform')
            == args.gui_platform)
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
