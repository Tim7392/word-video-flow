"""Counterexamples: project style edits must reach serialized editable text."""
import json
from types import SimpleNamespace

import pytest

from word_video.draft import export_draft
from word_video.media import executable, run
from word_video.template import default_styles


@pytest.fixture(scope='module')
def exports(tmp_path_factory):
    root = tmp_path_factory.mktemp('draft-style')
    bg = root / 'bg.mp4'
    run([executable('ffmpeg'), '-v', 'error', '-f', 'lavfi', '-i',
         'color=c=black:s=320x180:r=30:d=0.1', '-c:v', 'libx264', str(bg)])
    result = {}
    for label, factor in [('base', 1), ('larger', 1.5), ('smaller', .5)]:
        styles = default_styles()
        for style in styles.values():
            style.update(size=style['size'] * factor,
                         color='#123456' if label != 'base' else '#FFFFFF',
                         x=.3 if label != 'base' else .5,
                         y=.4 if label != 'base' else .5)
        manifest = SimpleNamespace(width=320, height=180, fps=30, total_frames=3,
            background=str(bg), intro_video='', intro_audio='', intro_frames=1,
            audio=[], styles=styles, title='Title', subtitle='Subtitle', footer='Footer',
            words=[dict(word='apple', phonetic='/apple/', meaning='meaning',
                        start_frame=1, male_frame=1, chinese_frame=1, end_frame=3)],
            to_dict=lambda: {})
        output = root / label
        export_draft(manifest, output)
        data = json.loads((output / 'draft_content.json').read_text(encoding='utf-8'))
        materials = {m['id']: json.loads(m['content'])['styles'][0]
                     for m in data['materials']['texts']}
        result[label] = {t['name']: (materials[t['segments'][0]['material_id']],
                                    t['segments'][0]['clip'])
                         for t in data['tracks'] if t['type'] == 'text' and t['segments']}
    return result


@pytest.mark.parametrize('role', tuple(default_styles()))
@pytest.mark.parametrize('direction', ['larger', 'smaller'])
def test_size_edit_reaches_draft(exports, role, direction):
    old = exports['base'][role][0]['size']
    new = exports[direction][role][0]['size']
    assert new > old if direction == 'larger' else new < old


@pytest.mark.parametrize('role', tuple(default_styles()))
def test_color_and_position_edits_reach_draft(exports, role):
    old_style, old_clip = exports['base'][role]
    style, clip = exports['larger'][role]
    assert style['fill'] != old_style['fill']
    assert clip['transform']['x'] == pytest.approx(-.4)
    assert clip['transform']['y'] != old_clip['transform']['y']
