"""Measure, instead of assume, what a frozen entry script sees (development tool).

``word_video_cli.py start`` re-enters itself to run the worker:

    subprocess.Popen([sys.executable, str(Path(__file__).resolve()), '--db', ...])

In a PyInstaller build ``sys.executable`` is the packaged exe and ``__file__``
is a path inside ``_internal``, so the worker command line carries one extra
argument that the source run does not have.  The member package must still
accept the CLI's own re-entry, and ``word_video_cli.py`` is not ours to change,
so ``packaging/pyi_rth_cli_argv.py`` normalises argv at startup.  This probe is
the evidence for that rule - it prints the frozen facts and re-enters itself the
same way the CLI does, so the printed child argv is the real thing:

    & <venv python> -m PyInstaller --noconfirm --onedir --console --noupx ^
        --name ProbeArgv --distpath <tmp>\\dist --workpath <tmp>\\work ^
        --specpath <tmp>\\spec packaging\\probe_frozen_argv.py
    <tmp>\\dist\\ProbeArgv\\ProbeArgv.exe spawn

It is never part of the member package.
"""
import json
import subprocess
import sys
from pathlib import Path


def facts(argv):
    return {'frozen': bool(getattr(sys, 'frozen', False)),
            'executable': sys.executable,
            'file': globals().get('__file__'),
            'argv': list(argv)}


def main(argv):
    record = {'self': facts(argv)}
    if len(argv) > 1 and argv[1] == 'spawn':
        child = subprocess.run([sys.executable, str(Path(__file__).resolve()),
                                '--db', 'x', 'work', '--job', 'y'],
                               capture_output=True, text=True,
                               encoding='utf-8', errors='replace')
        record['child'] = {'returncode': child.returncode,
                           'stdout': child.stdout.strip()[:2000],
                           'stderr': child.stderr.strip()[:1000]}
    return record


if __name__ == '__main__':
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    print(json.dumps(main(sys.argv), ensure_ascii=False, indent=1))
