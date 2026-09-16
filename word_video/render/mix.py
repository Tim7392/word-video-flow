"""Sample-accurate PCM assembly; prepared speech is never time-scaled again."""
import os
from pathlib import Path
import uuid
import wave

from ..media import executable,has_audio,run


def build_mix(manifest,target,cancel=None):
    target=Path(target).resolve()
    if target.exists(): raise FileExistsError(target)
    def check():
        if cancel and cancel(): raise RuntimeError('Audio assembly cancelled')
    check()
    target.parent.mkdir(parents=True,exist_ok=True)
    partial=target.with_name('.'+uuid.uuid4().hex+'.wav')
    intro=target.with_name('.intro-'+uuid.uuid4().hex+'.wav')
    sample=lambda frame:(frame*48000+manifest.fps//2)//manifest.fps
    events=[]
    # The intro clip usually carries its own audio; a separate intro_audio file is
    # the fallback.  A clip with no sound at all is valid input - the intro is then
    # simply silent - and must not fail the job the way it did before M0.
    candidates=[source for source in (manifest.intro_video,manifest.intro_audio) if source]
    intro_source=''
    for source in candidates:
        if has_audio(source):
            intro_source=source
            break
    if intro_source:
        if manifest.intro_frames<=0: raise ValueError('Intro sound without intro duration')
        run([executable('ffmpeg'),'-v','error','-nostdin','-n','-i',intro_source,
             '-t',str(manifest.intro_frames/manifest.fps),'-ar','48000','-ac','1','-c:a','pcm_s16le',intro])
        events.append((0,sample(manifest.intro_frames),intro))
    events.extend((sample(a['start_frame']),sample(a['start_frame']+a['duration_frames']),Path(a['path'])) for a in manifest.audio)
    events.sort(key=lambda e:e[0]);end=sample(manifest.total_frames)
    previous=0
    try:
        with wave.open(str(partial),'wb') as output:
            output.setnchannels(1);output.setsampwidth(2);output.setframerate(48000)
            def zeros(count):
                while count:
                    check(); chunk=min(count,48000)
                    output.writeframesraw(b'\0'*(chunk*2));count-=chunk
            for start,stop,path in events:
                check()
                if start<previous or stop<=start or stop>end: raise ValueError('Overlapping or out-of-range audio stage')
                zeros(start-previous)
                with wave.open(str(path),'rb') as source:
                    if (source.getnchannels(),source.getsampwidth(),source.getframerate())!=(1,2,48000):
                        raise ValueError('Prepared speech must be mono 48kHz signed16 PCM')
                    available=source.getnframes();capacity=stop-start
                    if available>capacity+1: raise ValueError('Speech longer than allocated stage')
                    remaining=min(available,capacity)
                    while remaining:
                        check();frames=min(remaining,48000);block=source.readframes(frames)
                        if len(block)!=frames*2: raise ValueError('Truncated WAV data')
                        output.writeframesraw(block);remaining-=frames
                    zeros(capacity-min(available,capacity))
                previous=stop
            zeros(end-previous)
        check();os.link(partial,target)
        return str(target)
    finally:
        partial.unlink(missing_ok=True);intro.unlink(missing_ok=True)
