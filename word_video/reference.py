"""Read-only extraction of the known reference's editable inner composition."""
from dataclasses import asdict
import json
import re
from pathlib import Path

from .contracts import WordEntry, SpeechAsset, LessonSpec
from .media import prepare_audio, atomic_json
from .template import default_styles

VOICE_ROLES = {'Energetic Female(English)': 'female',
               'Energetic Male(English)': 'male', '网文解说': 'chinese'}


def extract_reference(root, output, count=3, first_index=301, speed=1.25):
    root = Path(root).resolve(strict=True)
    output = Path(output).resolve()
    if output.exists():
        raise FileExistsError(output)
    nested = root/'subdraft/C90F687F-A293-4d48-887F-761242D9E3EC/draft_content.json'
    inner = json.loads(nested.read_text(encoding='utf-8'))['materials']['drafts'][0]['draft']
    texts = {x['id']: json.loads(x['content'])['text'] for x in inner['materials']['texts']}
    tracks = [sorted(t['segments'], key=lambda s:s['target_timerange']['start'])
              for t in inner['tracks'] if t['type']=='text' and t['segments']]
    if len(tracks)!=3 or any(len(t)!=50 for t in tracks):
        raise ValueError('Reference text structure changed')
    if not 1 <= count <= 50:
        raise ValueError('Reference has 50 words; count must be 1..50')
    materials = {a['id']:a for a in inner['materials']['audios']}
    speech = {role:[] for role in VOICE_ROLES.values()}
    for track in inner['tracks']:
        if track['type']!='audio': continue
        for segment in track['segments']:
            material=materials[segment['material_id']]
            role=VOICE_ROLES.get(material['tone_effect_name'])
            if role:
                speech[role].append((segment['target_timerange']['start'], material))
    for role in speech:
        speech[role].sort(key=lambda x:x[0])
        if len(speech[role])!=50:
            raise ValueError('Reference voice count changed: '+role)
    entries=[]
    source_assets=[]
    for pos in range(count):
        word, phonetic, meaning = [texts[t[pos]['material_id']] for t in tracks]
        from subtitle_factory_core import _pos_pattern
        chinese=_pos_pattern.sub('',meaning).strip()
        entry=WordEntry(first_index+pos, word, phonetic, meaning, chinese)
        entries.append(entry)
        for role in speech:
            material=speech[role][pos][1]
            expected=chinese if role=='chinese' else word
            displayed=material['name']
            normalize=lambda text: re.sub(r'\s+','',text)
            matches=(normalize(expected).startswith(normalize(displayed[:-3])) if displayed.endswith('...')
                     else normalize(displayed)==normalize(expected))
            expected_start=tracks[{'female':0,'male':1,'chinese':2}[role]][pos]['target_timerange']['start']
            if not matches or abs(speech[role][pos][0]-expected_start)>17000:
                raise ValueError(f'Reference voice/text mismatch: {entry.index}/{role}')
            path=root/'textReading'/Path(material['path']).name
            if not path.is_file(): raise FileNotFoundError(path)
            source_assets.append((entry,role,material,path))
    background=root/'Resources/videoAlg/6bc2db04022c487a9d17f92d83b6a2c9_0_59250000.mp4'
    background.resolve(strict=True)
    output.mkdir(parents=True)
    assets=[]
    for entry,role,material,source in source_assets:
        destination=output/f'{entry.index:04d}_{role}.wav'
        raw,rendered=prepare_audio(source,destination,speed)
        spoken=entry.spoken_meaning if role=='chinese' else entry.word
        assets.append(SpeechAsset(entry.index,role,spoken,str(destination),raw,rendered,
                                  material['tone_effect_name']))
    lesson=LessonSpec(entries=entries,speed=speed,background=str(background),styles=default_styles())
    atomic_json(output/'lesson.json',asdict(lesson))
    atomic_json(output/'speech.json',[asdict(a) for a in assets])
    return lesson,assets
