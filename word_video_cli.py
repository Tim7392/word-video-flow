"""Headless vocabulary-video JSON CLI; no GUI modules are imported."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys

from word_video import jobs

DEFAULT_DB=Path(__file__).resolve().parent/'word_video_work/jobs.sqlite3'


def chosen(raw):
    """Parse --voices JSON into a role -> speaker map, resolving names too."""
    if not raw:
        return None
    from word_video.voices import as_role_map
    return as_role_map(json.loads(raw))


class JsonArgumentParser(argparse.ArgumentParser):
    """Argument errors must still leave one JSON object on stdout.

    The contract is "stdout is a single JSON + an exit code"; argparse's default
    is a usage line on stderr and nothing on stdout, which callers cannot read.
    Exit code 2 is kept.
    """

    def error(self, message):
        raise ValueError('%s\n%s' % (message, self.format_usage().strip()))


def build_parser():
    parser = JsonArgumentParser(description=__doc__)
    parser.add_argument('--db', default=str(DEFAULT_DB))
    commands = parser.add_subparsers(dest='action', required=True)
    commands.add_parser('doctor')
    commands.add_parser('schema')
    voices = commands.add_parser('voices')
    voices.add_argument('--root', action='append', default=None,
                        help='draft folder to inspect; repeatable, defaults to the client folders')
    audition = commands.add_parser('tts-audition')
    audition.add_argument('--output', required=True)
    audition.add_argument('--voices', default=None,
                          help='JSON object role->speaker id (or name); default is the three originals')
    jy_audition = commands.add_parser('jianying-audition')
    jy_audition.add_argument('--output', required=True)
    jy_audition.add_argument('--voices', default=None,
                             help='JSON object role->speaker id (or name); default is the three originals')
    submit = commands.add_parser('submit')
    submit.add_argument('--request', required=True)
    for action in ('status', 'work', 'start', 'pause', 'resume', 'cancel'):
        child = commands.add_parser(action)
        child.add_argument('--job', required=True)
    relocate = commands.add_parser('relocate')
    relocate.add_argument('--draft', required=True)
    inspect = commands.add_parser('draft-state')
    inspect.add_argument('--draft', required=True)
    return parser, commands


def main(argv=None):
    parser, commands = build_parser()
    args = parser.parse_args(argv)
    if args.action == 'schema':
        return {'version': 1, 'actions': sorted(commands.choices),
                'request':{'idempotency_key':'stable-name','output':'absolute output directory',
                    'source':{'path':'absolute DOCX/TXT path'},
                    'lesson':{'background':'absolute MP4 path','batch_size':50,'speed':1.25},
                    'provider':{'kind':'volcengine','voices':{'female':'approved ID','male':'approved ID','chinese':'approved ID'},'voices_confirmed':True}},
                'notes':['Use provider local with items[index,role,text,voice,path] for original unscaled audio.',
                         'voices lists every reading voice this machine has used, read from the tone_speaker field of each readable Jianying draft; use it to pick the voice you already know, then audition it before a batch.',
                         'A chosen voice works on both routes: provider.voices names one id per role and is sent verbatim; the service decides entitlement, and a refusal fails the job instead of being substituted.',
                         'volcengine_original fixes the three original BV IDs via legacy API; tts-audition then explicit voices_confirmed required. Live parity not yet verified.',
                         'jianying_original uses the installed JianyingPro client reading channel with the same three original IDs; jianying-audition to listen first. Internal endpoint, no credentials.',
                         'parallel runs routes side by side (jianying + volcengine_legacy), keeps every route audio in the cache and records timeline_route; a route failure is reported, never substituted.',
                         'provider.concurrency (1..100, default 1) is how many utterances are worked on at once; provider.route_concurrency overrides the per-route ceiling. Measured 2026-09-16: the paid legacy endpoint sustains ~50 calls/s at 100 in flight while the Jianying channel flattens at ~7 calls/s. Tokens and cache layout stay identical to a serial run.',
                         'volcengine voices are account-entitled speaker ids, including voices the user cloned in the console; an unentitled id fails the job, never falls back.',
                         'prepared_speech is an alternative for already tempo-adjusted SpeechAsset records.',
                         'generated means files produced and structurally checked; physical acceptance remains separate.',
                         'resume clears pause/cancel; invoke start again when previous worker has stopped.']}
    if args.action=='doctor':
        from word_video.media import executable
        from word_video.draft import _load_library
        from word_video.original_tts import readiness
        from word_video.template import font_report
        _load_library()
        fonts=font_report()
        return {'ffmpeg':executable('ffmpeg'),'ffprobe':executable('ffprobe'),
                'tts_key_configured':bool(os.environ.get('MODEL_SPEECH_API_KEY')),
                'original_tts':readiness(),
                'fonts':fonts,
                'font_substitutions':sorted(k for k,v in fonts.items() if v['source']=='substitute'),
                'font_missing':sorted(k for k,v in fonts.items() if v['source']=='missing'),
                'ui_modules_loaded':any(n.startswith(('uiautomation','pyautogui','PySide','PyQt')) for n in sys.modules)}
    if args.action=='voices':
        from word_video.voices import discover
        return discover(args.root)
    if args.action=='tts-audition':
        from word_video.original_tts import audition
        return audition(args.output, chosen(args.voices))
    if args.action=='jianying-audition':
        from word_video.jianying_tts import audition as jianying_audition
        return jianying_audition(args.output, chosen(args.voices))
    if args.action=='submit':
        return jobs.submit(args.db,json.loads(Path(args.request).read_text(encoding='utf-8-sig')))
    if args.action=='status': return jobs.status(args.db,args.job)
    if args.action=='work': return jobs.work(args.db,args.job)
    if args.action in ('pause','cancel','resume'):
        return jobs.control(args.db,args.job,{'resume':'run'}.get(args.action,args.action))
    if args.action=='relocate':
        from word_video.draft import relocate_draft
        relocate_draft(args.draft)
        return {'draft':str(Path(args.draft).resolve()),'relocated':True}
    if args.action=='draft-state':
        from word_video.draft import draft_state
        return draft_state(args.draft)
    if args.action=='start':
        state=jobs.status(args.db,args.job)
        if state['state'] in ('running','generated'): return state
        if state['control']!='run': raise ValueError('Job paused/cancelled; use resume first')
        log=Path(args.db).resolve().parent/(args.job+'.worker.log')
        kwargs={'creationflags':subprocess.CREATE_NO_WINDOW|subprocess.CREATE_NEW_PROCESS_GROUP|subprocess.DETACHED_PROCESS} if os.name=='nt' else {'start_new_session':True}
        with log.open('ab') as stream:
            process=subprocess.Popen([sys.executable,str(Path(__file__).resolve()),'--db',str(Path(args.db).resolve()),'work','--job',args.job],
                stdin=subprocess.DEVNULL,stdout=stream,stderr=stream,cwd=str(Path(__file__).resolve().parent),**kwargs)
        return {'id':args.job,'state':'starting','pid':process.pid,'log':str(log)}


if __name__=='__main__':
    if hasattr(sys.stdout,'reconfigure'): sys.stdout.reconfigure(encoding='utf-8')
    try:
        response=main()
        if isinstance(response,dict) and isinstance(response.get('result'),dict) and 'files' in response['result']:
            records=response['result']['files']
            response['result']['file_count']=len(records)
            response['result']['files']=[{'path':x['path']} for x in records
                if Path(x['path']).suffix in ('.srt','.mp4') or Path(x['path']).name in ('draft_content.json','draft_meta_info.json')]
        print(json.dumps({'ok':True,'result':response},ensure_ascii=False,allow_nan=False))
    except SystemExit:
        raise                     # --help / --version keep argparse's own exit
    except ValueError as error:
        # Bad arguments: still one JSON object, still exit code 2.
        print(json.dumps({'ok':False,'error':{'type':'ArgumentError','message':str(error)}},ensure_ascii=False))
        sys.exit(2)
    except Exception as error:
        print(json.dumps({'ok':False,'error':{'type':type(error).__name__,'message':str(error)}},ensure_ascii=False))
        sys.exit(1)
