"""Counterexamples: project style edits must reach serialized editable text."""
import json
from types import SimpleNamespace

import pytest

from word_video.draft import export_draft
from word_video.media import executable, run
from word_video.template import default_styles, draft_text_size, style_size


@pytest.fixture(scope='module')
def bg(tmp_path_factory):
    root = tmp_path_factory.mktemp('draft-style')
    path = root / 'bg.mp4'
    run([executable('ffmpeg'), '-v', 'error', '-f', 'lavfi', '-i',
         'color=c=black:s=320x180:r=30:d=0.1', '-c:v', 'libx264', str(path)])
    return path


def manifest(bg, styles, words=None):
    return SimpleNamespace(width=320, height=180, fps=30, total_frames=3,
        background=str(bg), intro_video='', intro_audio='', intro_frames=1,
        audio=[], styles=styles, title='Title', subtitle='Subtitle', footer='Footer',
        words=words if words is not None else [
            dict(word='apple', phonetic='/apple/', meaning='meaning',
                 start_frame=1, male_frame=1, chinese_frame=1, end_frame=3)],
        to_dict=lambda: {})


def read_materials(output):
    data = json.loads((output / 'draft_content.json').read_text(encoding='utf-8'))
    materials = {m['id']: json.loads(m['content'])['styles'][0]
                 for m in data['materials']['texts']}
    return {t['name']: (materials[t['segments'][0]['material_id']],
                        t['segments'][0]['clip'])
            for t in data['tracks'] if t['type'] == 'text' and t['segments']}


@pytest.fixture(scope='module')
def exports(bg):
    result = {}
    for label, factor in [('base', 1), ('larger', 1.5), ('smaller', .5)]:
        styles = default_styles()
        for style in styles.values():
            style.update(size=style['size'] * factor,
                         color='#123456' if label != 'base' else '#FFFFFF',
                         x=.3 if label != 'base' else .5,
                         y=.4 if label != 'base' else .5)
        output = bg.parent / label
        export_draft(manifest(bg, styles), output)
        result[label] = read_materials(output)
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
    assert clip['transform']['y'] == pytest.approx(.2)
    assert style['fill']['content']['solid']['color'] == pytest.approx(
        [0x12 / 255, 0x34 / 255, 0x56 / 255])


@pytest.mark.parametrize('role', tuple(default_styles()))
@pytest.mark.parametrize('value', [315, 450.0, '240', .5, 20000])
def test_round_trip(role, value):
    assert style_size(draft_text_size(value, role), role) == pytest.approx(
        float(value), rel=1e-12, abs=1e-12)


@pytest.mark.parametrize('bad', [0, -1, None, 'abc', float('nan'), float('inf')])
def test_rejects_non_positive_or_non_finite(bad):
    with pytest.raises(ValueError):
        draft_text_size(bad, 'title')
    with pytest.raises(ValueError):
        style_size(bad, 'title')


def test_legacy_draft_size_field_has_no_render_say(bg, exports):
    """The old draft_size knob must not be a second rendering authority."""
    styles = default_styles()
    for style in styles.values():
        style['draft_size'] = 99  # would dwarf every layer if still honoured
    output = bg.parent / 'legacy'
    export_draft(manifest(bg, styles), output)
    # Resource paths are unique per export; base also deliberately overrides
    # color/position. Compare the size field this counterexample is about.
    actual = read_materials(output)
    for role in styles:
        assert actual[role][0]['size'] == exports['base'][role][0]['size']


@pytest.mark.parametrize('role', tuple(default_styles()))
@pytest.mark.parametrize('label,factor', [('base', 1), ('larger', 1.5), ('smaller', .5)])
def test_serialized_size_uses_role_mapping(exports, role, label, factor):
    expected = draft_text_size(default_styles()[role]['size'] * factor, role)
    assert exports[label][role][0]['size'] == pytest.approx(expected)


@pytest.mark.parametrize('role', tuple(default_styles()))
def test_serialized_bold_and_argb(bg, role):
    styles = {role: dict(color='#80123456', bold=not default_styles()[role]['bold'])}
    output = bg.parent / ('argb-' + role)
    export_draft(manifest(bg, styles), output)
    material, clip = read_materials(output)[role]
    assert material['bold'] is styles[role]['bold']
    assert material['fill']['content']['solid']['color'] == pytest.approx(
        [0x12 / 255, 0x34 / 255, 0x56 / 255])
    assert clip['alpha'] == 1  # alpha ignored, matching unchanged ASS semantics


@pytest.mark.parametrize('role,size,expected', [
    ('title', 315, 9 * 126 / 94), ('subtitle', 240, 7 * 93 / 72),
    ('footer', 210, 6 * 69 / 51), ('english', 450, 13),
    ('phonetic', 174, 5), ('meaning', 210, 6), ('countdown', 675, 20)])
def test_sample_coefficients_and_uncalibrated_defaults(role, size, expected):
    assert draft_text_size(size, role) == pytest.approx(expected)


def test_countdown_animation_override_reaches_keyframes(bg):
    output = bg.parent / 'pulse'
    export_draft(manifest(bg, {'countdown': {'animation': 'candidate_pulse'}}), output)
    data = json.loads((output / 'draft_content.json').read_text(encoding='utf-8'))
    track = next(t for t in data['tracks'] if t['name'] == 'countdown')
    frames = track['segments'][0]['common_keyframes']
    assert len(frames) == 2
    assert {frame['property_type'] for frame in frames} == {'KFTypeScaleX', 'KFTypeAlpha'}
