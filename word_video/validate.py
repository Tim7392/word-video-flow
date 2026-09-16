"""Structural evidence only; does not claim physical Jianying acceptance."""
import json
from pathlib import Path
from .media import probe


def validate_draft(folder, manifest):
    root=Path(folder).resolve(strict=True)
    data=json.loads((root/'draft_content.json').read_text(encoding='utf-8'))
    tracks={x['name']:x for x in data['tracks']}
    n=len(manifest.words)
    for name in ('english','phonetic','meaning'):
        expected=sum(bool(x[{'english':'word','phonetic':'phonetic','meaning':'meaning'}[name]]) for x in manifest.words)
        if len(tracks[name]['segments'])!=expected: raise ValueError('Wrong text count: '+name)
    for role in ('female','male','chinese'):
        if len(tracks[role]['segments'])!=n: raise ValueError('Wrong speech count: '+role)
    refs={m['id'] for group in data['materials'].values() if isinstance(group,list)
          for m in group if isinstance(m,dict) and 'id' in m}
    end=0
    for track in data['tracks']:
        for item in track['segments']:
            if item['material_id'] not in refs: raise ValueError('Dangling material reference')
            r=item['target_timerange']; end=max(end,r['start']+r['duration'])
    expected=round(manifest.total_frames*1e6/manifest.fps)
    if abs(end-expected)>1 or abs(data['duration']-expected)>1:
        raise ValueError('Draft end differs from manifest')
    paths=[]
    def visit(value):
        if isinstance(value,dict):
            for key,item in value.items():
                if key in ('path','font_path') and isinstance(item,str) and item:
                    p=Path(item)
                    if not p.is_file() or not p.resolve().is_relative_to(root):
                        raise ValueError('Missing or external draft asset: '+item)
                    paths.append(str(p))
                else: visit(item)
        elif isinstance(value,list):
            for item in value: visit(item)
        elif isinstance(value,str) and value.startswith('{'):
            try: visit(json.loads(value))
            except json.JSONDecodeError: pass
    visit(data['materials'])
    return {'structural_ok':True,'physical_editability':'PENDING_USER_OPEN',
            'words':n,'speech_segments':n*3,'text_segments':sum(len(tracks[x]['segments']) for x in ('english','phonetic','meaning')),
            'asset_files':len(set(paths)),'duration_s':expected/1e6,'draft':str(root)}


def validate_video(path, manifest):
    data=probe(path)
    video=next(s for s in data['streams'] if s['codec_type']=='video')
    audio=next((s for s in data['streams'] if s['codec_type']=='audio'),None)
    if not audio: raise ValueError('Video has no audio stream')
    if (video['width'],video['height'])!=(manifest.width,manifest.height): raise ValueError('Wrong video canvas')
    if abs(float(data['format']['duration'])-manifest.total_frames/manifest.fps)>1/manifest.fps+.001:
        raise ValueError('Video duration differs from timeline')
    return {'structural_ok':True,'duration_s':float(data['format']['duration']),
            'width':video['width'],'height':video['height'],'audio_codec':audio['codec_name']}
