"""SQLite-backed generation jobs, cooperative control and verified checkpoints."""
from contextlib import contextmanager
from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import time
import uuid

from .contracts import LessonSpec, WordEntry, SpeechAsset, TimelineManifest
from .media import atomic_json, sha256
from . import text_policy
from .template import default_styles


@contextmanager
def connect(db):
    db=Path(db).resolve(); db.parent.mkdir(parents=True,exist_ok=True)
    con=sqlite3.connect(db,timeout=30)
    con.row_factory=sqlite3.Row
    con.execute('CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, request TEXT NOT NULL, state TEXT NOT NULL, control TEXT NOT NULL, result TEXT, error TEXT, updated REAL NOT NULL)')
    con.commit()
    try:
        with con: yield con
    finally:
        con.close()


@contextmanager
def job_lock(db, job):
    path=Path(str(Path(db).resolve())+'.'+job+'.lock')
    stream=path.open('a+b')
    stream.seek(0)
    if path.stat().st_size==0: stream.write(b'0'); stream.flush(); stream.seek(0)
    try:
        if os.name=='nt':
            import msvcrt
            msvcrt.locking(stream.fileno(),msvcrt.LK_NBLCK,1)
        else:
            import fcntl
            fcntl.flock(stream,fcntl.LOCK_EX|fcntl.LOCK_NB)
    except OSError:
        stream.close(); raise RuntimeError('Job already has a running worker') from None
    try: yield
    finally: stream.close()


def lesson_from_request(request):
    settings=dict(request.get('lesson',{}))
    requested=settings.pop('spoken_policy',None)
    has_entries='entries' in settings
    if requested is not None:
        # Any recorded value is accepted here so a stored request replays; which of
        # them is meaningful for *this* request is decided below.
        text_policy.check_policy(requested,allow_supplied=True)
    if has_entries:
        # The request brings its own reading text, so nothing is re-derived here:
        # that is also how a stored request is replayed (submit freezes
        # ``asdict(lesson)``, work reads it back).  The recorded policy is what the
        # text was produced with, or 'supplied' when the caller did not say.
        settings['entries']=[WordEntry(**x) for x in settings['entries']]
        settings['spoken_policy']=requested or text_policy.POLICY_SUPPLIED
    else:
        if requested==text_policy.POLICY_SUPPLIED:
            raise ValueError("spoken_policy 'supplied' needs lesson.entries: "
                             "there is no supplied reading text to keep")
        policy=requested or text_policy.DEFAULT_POLICY
        from subtitle_factory_api import load_words, selection
        rows,_=load_words(request['source'])
        rows,selected=selection(rows,request.get('range',{}))
        # Only the derived reading field is cleaned; the display meaning (d_f) and
        # the protected legacy core are untouched.  Deriving it here - rather than
        # taking d_c - is what makes the produced batch and the independent
        # acceptance tool apply one rule.
        settings['entries']=[WordEntry(i,r['w'],r['p'],r['d_f'],
                                       text_policy.spoken_from_meaning(r['d_f'],policy))
                             for i,r in enumerate(rows,selected['start'])]
        settings['spoken_policy']=policy
    settings.setdefault('styles',default_styles())
    # A supplied partial styles map must not skip font resolution, and a font
    # that resolves to a substitute (Jianying purges its effect cache) is
    # recorded in the manifest instead of failing the whole job.
    resolved=default_styles()
    for name,value in (settings['styles'] or {}).items():
        resolved.setdefault(name,{}).update(value or {})
    settings['styles']=resolved
    settings.setdefault('fonts',{role:resolved[role].get('font') for role in resolved})
    lesson=LessonSpec(**settings)
    from .timing import split_lesson
    split_lesson(lesson)
    if not .5<=lesson.speed<=2: raise ValueError('speed must be .5..2')
    if lesson.width%2 or lesson.height%2: raise ValueError('H.264 canvas dimensions must be even')
    Path(lesson.background).resolve(strict=True)
    return lesson


def submit(db, request):
    allowed={'idempotency_key','output','source','range','lesson','provider','prepared_speech'}
    if set(request)-allowed: raise ValueError('Unknown request fields')
    # Resolve vocabulary at submission: edits to the source cannot silently alter a queued job.
    lesson=lesson_from_request(request)
    normalized={'lesson':asdict(lesson),'output':str(Path(request['output']).resolve()),
                'provider':request.get('provider'), 'prepared_speech':request.get('prepared_speech')}
    if bool(normalized['provider'])==bool(normalized['prepared_speech']):
        raise ValueError('Supply exactly one provider or prepared_speech')
    if normalized['provider']:
        provider=normalized['provider']
        allowed_provider={'kind','items','voices','voices_confirmed','resource','routes','timeline_route','concurrency','route_concurrency'}
        if set(provider)-allowed_provider: raise ValueError('Unknown provider field; credentials belong in environment only')
        if provider.get('kind') not in ('local','volcengine','volcengine_original','jianying_original','volcengine_bigmodel','parallel'): raise ValueError('Unknown provider kind')
    inputs={str(Path(lesson.background).resolve())}
    if lesson.intro_audio: inputs.add(str(Path(lesson.intro_audio).resolve(strict=True)))
    for key in ('video','audio'):
        clip=Path((lesson.intro or {}).get(key) or '').resolve(strict=True) if (lesson.intro or {}).get(key) else None
        if clip: inputs.add(str(clip))
    for style in lesson.styles.values():
        font_path=style.get('font')
        if font_path: inputs.add(str(Path(font_path).resolve(strict=True)))
    for asset in normalized['prepared_speech'] or []: inputs.add(str(Path(asset['path']).resolve(strict=True)))
    for asset in (normalized['provider'] or {}).get('items',[]): inputs.add(str(Path(asset['path']).resolve(strict=True)))
    normalized['input_files']={p:sha256(p) for p in sorted(inputs)}
    key=request.get('idempotency_key')
    if not isinstance(key,str) or not key.strip(): raise ValueError('idempotency_key required')
    job='wv-'+hashlib.sha256(key.encode()).hexdigest()[:24]
    raw=json.dumps(normalized,ensure_ascii=False,sort_keys=True,allow_nan=False)
    fingerprint=hashlib.sha256(raw.encode()).hexdigest()
    with connect(db) as con:
        old=con.execute('SELECT fingerprint FROM jobs WHERE id=?',(job,)).fetchone()
        if old:
            if old['fingerprint']!=fingerprint: raise ValueError('IDEMPOTENCY_CONFLICT')
        else:
            con.execute('INSERT INTO jobs VALUES (?,?,?,?,?,?,?,?)',
                        (job,fingerprint,raw,'queued','run',None,None,time.time()))
    return status(db,job)


def status(db,job):
    with connect(db) as con:
        row=con.execute('SELECT id,state,control,result,error,updated FROM jobs WHERE id=?',(job,)).fetchone()
    if not row: raise ValueError('Unknown job')
    result=dict(row)
    if result['result']: result['result']=json.loads(result['result'])
    if result['state']=='running':
        try:
            with job_lock(db,job): result['state']='interrupted'
        except RuntimeError: pass
    return result


def control(db,job,action):
    if action not in ('pause','cancel','run'): raise ValueError('Unknown job control')
    with connect(db) as con:
        row=con.execute('SELECT state FROM jobs WHERE id=?',(job,)).fetchone()
        if not row: raise ValueError('Unknown job')
        if row['state']=='generated': raise ValueError('Job already generated')
        con.execute('UPDATE jobs SET control=?,updated=? WHERE id=?',(action,time.time(),job))
    return status(db,job)


class JobControl(Exception): pass


def work(db,job):
    with job_lock(db,job):
        with connect(db) as con:
            row=con.execute('SELECT * FROM jobs WHERE id=?',(job,)).fetchone()
            if not row: raise ValueError('Unknown job')
            request=json.loads(row['request'])
            if row['state']=='generated':
                saved=json.loads(row['result'])
                for item in saved['files']:
                    if sha256(item['path'])!=item['sha256']: raise ValueError('OUTPUT_CHANGED')
                return status(db,job)
            con.execute('UPDATE jobs SET state=?,error=NULL,updated=? WHERE id=?',('running',time.time(),job))
        def checkpoint():
            with connect(db) as con:
                action=con.execute('SELECT control FROM jobs WHERE id=?',(job,)).fetchone()['control']
            if action!='run': raise JobControl(action)
        try:
            from .timing import split_lesson,build_timeline
            from .tts import build_speech
            from .draft import export_draft
            from .srt_export import export_srts
            from .render import render_video
            from .validate import validate_draft,validate_video
            for path,digest in request['input_files'].items():
                if sha256(path)!=digest: raise ValueError('INPUT_CHANGED: '+path)
            lesson=lesson_from_request(request)
            root=Path(request['output'])/job
            root.mkdir(parents=True,exist_ok=True)
            owner=root/'job-owner.json'
            if owner.exists():
                if json.loads(owner.read_text(encoding='utf-8'))['fingerprint']!=row['fingerprint']:
                    raise ValueError('Output belongs to a different request')
            elif any(root.iterdir()): raise ValueError('Output folder is not empty')
            else: atomic_json(owner,{'fingerprint':row['fingerprint']})
            all_files=[]; batches=[]
            # Wall-clock per stage, so a slow run can be diagnosed instead of
            # guessed at.  Values are summed across batches.
            timing={}
            def spend(phase, started):
                timing[phase]=timing.get(phase,0.0)+round(time.monotonic()-started,3)
            reported=[0.0]
            def report_progress(value):
                """Publish stage progress at most once a second, for long stages."""
                if time.monotonic()-reported[0]<1: return
                reported[0]=time.monotonic()
                with connect(db) as con:
                    con.execute('UPDATE jobs SET result=?,updated=? WHERE id=?',
                                (json.dumps(dict(value),ensure_ascii=False),time.time(),job))
            for batch in split_lesson(lesson):
                checkpoint()
                folder=root/f'{batch.entries[0].index:04d}-{batch.entries[-1].index:04d}'
                committed=folder/'complete.json'
                if committed.exists():
                    saved=json.loads(committed.read_text(encoding='utf-8'))
                    for item in saved['files']:
                        if sha256(item['path'])!=item['sha256']: raise ValueError('OUTPUT_CHANGED')
                    all_files.extend(saved['files']); batches.append(saved['batch']); continue
                if folder.exists():
                    # Only this job's uncommitted batch is moved, not deleted or reused.
                    recovery=root/'recovery'; recovery.mkdir(exist_ok=True)
                    folder.rename(recovery/(folder.name+'-'+uuid.uuid4().hex))
                folder.mkdir()
                speech_started=time.monotonic()
                if request['prepared_speech']:
                    indexes={x.index for x in batch.entries}
                    assets=[SpeechAsset(**x) for x in request['prepared_speech'] if x['word_index'] in indexes]
                else:
                    assets=build_speech(batch,request['provider'],root/'audio-cache',
                                        checkpoint,
                                        progress=lambda value:report_progress(
                                            dict(value,first=batch.entries[0].index,
                                                 last=batch.entries[-1].index)))
                spend('speech',speech_started)
                timeline_started=time.monotonic()
                manifest=build_timeline(batch,assets)
                # Provenance in the published timeline.json: which rule produced the
                # reading text.  The full lesson carries it; a split batch does not.
                manifest.spoken_policy=lesson.spoken_policy
                timeline=folder/'timeline.json'
                atomic_json(timeline,manifest.to_dict())
                spend('timeline',timeline_started)
                draft_started=time.monotonic()
                draft=folder/'editable-draft'
                if not draft.exists(): export_draft(manifest,draft)
                draft_report=validate_draft(draft,manifest)
                spend('draft',draft_started)
                checkpoint()
                srt_started=time.monotonic()
                srts=folder/'srt'
                if not srts.exists(): export_srts(manifest,srts)
                srt_files=list(srts.glob('*.srt'))
                if len(srt_files)!=5: raise ValueError('Incomplete SRT output; preserve and inspect')
                spend('srt',srt_started)
                checkpoint()
                video=folder/'video'
                video_file=video/'video.mp4'
                if not video_file.exists():
                    render_started=time.monotonic()
                    render_video(manifest,video,cancel=lambda:_cancel_requested(db,job),
                                 progress=lambda value:report_progress(
                                     dict(value,first=manifest.first_index,
                                          last=manifest.last_index)))
                    spend('render',render_started)
                checkpoint()
                video_report=validate_video(video_file,manifest)
                files=[p for p in folder.rglob('*') if p.is_file() and p!=committed]
                records=[{'path':str(p),'sha256':sha256(p)} for p in files]
                batch_result={'first':manifest.first_index,'last':manifest.last_index,
                              'draft':draft_report,'video':str(video_file),'video_check':video_report}
                atomic_json(committed,{'files':records,'batch':batch_result})
                all_files.extend(records); batches.append(batch_result)
            result={'files':all_files,'batches':batches,'physical_acceptance':'PENDING_USER_OPEN_AND_LISTEN',
                    'stage_seconds':dict(sorted(timing.items(),key=lambda kv:-kv[1])),
                    'spoken_policy':lesson.spoken_policy,
                    'intro_clip':'PROVIDED' if lesson.intro else 'NOT_PROVIDED',
                    'original_countdown':('REFERENCE_CLIP_EMBEDDED' if lesson.intro
                                          else 'NOT_IMPLEMENTED' if lesson.intro_s else 'OMITTED_BY_REQUEST'),
                    'intro_sound':_intro_sound_report(lesson)}
            with connect(db) as con:
                con.execute('UPDATE jobs SET state=?,result=?,updated=? WHERE id=?',
                            ('generated',json.dumps(result,ensure_ascii=False),time.time(),job))
        except BaseException as error:
            action=None
            with connect(db) as con:
                action=con.execute('SELECT control FROM jobs WHERE id=?',(job,)).fetchone()['control']
                state={'pause':'paused','cancel':'cancelled'}.get(action,'failed')
                con.execute('UPDATE jobs SET state=?,error=?,updated=? WHERE id=?',
                            (state,str(error)[:1500],time.time(),job))
            if state=='failed': raise
    return status(db,job)


def _cancel_requested(db,job):
    with connect(db) as con:
        return con.execute('SELECT control FROM jobs WHERE id=?',(job,)).fetchone()['control']!='run'


def _intro_sound_report(lesson):
    """Which file really supplies the intro's sound, from the one resolver.

    The previous report said ``FROM_CLIP`` whenever a clip existed, so a silent
    clip hiding a configured ``intro_audio`` was reported as the clip's sound,
    and a clip whose sound came from the fallback file was indistinguishable
    from one carrying its own countdown.  The resolver answers the same question
    for the MP4, the draft and this report; when a configured file is not the
    one used, the value says so instead of implying it was heard.
    """
    from .media import resolve_intro_audio
    clip = dict(lesson.intro or {})
    if not clip and not lesson.intro_audio:
        return 'NOT_CONFIGURED'
    choice = resolve_intro_audio(clip.get('video'), clip.get('audio'),
                                 lesson.intro_audio)
    if choice.source == 'FROM_CLIP':
        return 'FROM_CLIP'
    if choice.source == 'FROM_INTRO_AUDIO':
        # A configured file that nothing can audition is not an approval.
        return 'FROM_INTRO_AUDIO_NOT_AUDITIONED'
    if lesson.intro_audio:
        return 'SILENT_CLIP_INTRO_AUDIO_NOT_USED'
    return 'SILENT_CLIP_NO_INTRO_AUDIO'
