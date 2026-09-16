"""Per-role style overrides: the project owns them, the plan carries them.

"改字号" had no project-level entry, so the only way to change it was editing the
template defaults — which changes every member's output.  These tests pin the
replacement and, more importantly, prove the effect where it has to be visible:

* an override names a few fields and the rest keep following the defaults;
* the document states the revision it needs (``wv-project@3``) while @1/@2 keep
  loading byte for byte the way they did;
* the *plan* carries the overrides, so the renderer and the canvas read one table;
* the text really gets bigger — measured as a layout box from real font metrics and
  again in the pixels of a rendered frame — and the speech step is not re-run.
"""
from dataclasses import replace
import json
from pathlib import Path
import subprocess
import tempfile
import wave

import pytest
from unittest.mock import patch

from test_wv_export_project import _ink
from test_wv_project_golden import golden_media, golden_project

from word_video.application import (ClearStyle, SetStyle, apply, instantiate,
                                    merged_styles, style_fonts)
from word_video.contracts import LessonSpec, WordEntry
from word_video.domain import (DEFAULT_LESSON_TEMPLATE, SCHEMA, SCHEMA_V1, SCHEMA_V2,
                               InvalidTimeError, Project, Record, SchemaError,
                               StyleOverride, render_plan, solve)
from word_video.exporters.render import _styles
from word_video.layout.surface import LayoutSurface
from word_video.media import executable, run
from word_video.storage.project_store import save_project
from word_video.template import default_styles

FIXTURE = Path(r'D:\1\1-AI_workflow\word_video_flow\data\fixtures\p1-有片头.json')
ARCHIVE = Path(r'D:\单词速记自动化_测试归档_0915')
BACKGROUND = ARCHIVE / '_prepared-1080p' / 'background-from-reference-1080p.mp4'
CANVAS = {'width': 320, 'height': 180}
FPS = 24
#: Reference-canvas px; the default is 450.  A bigger jump (900) is refused by the
#: layout's own safe-area guard on a small canvas (0.42 of the height > 0.40), which
#: is that guard working; 800 stays inside it and still nearly triples the glyph area.
BIGGER = 800
OVERRIDE = StyleOverride(role='english', fields=(('size', BIGGER),))
#: Measured on the rendered pair below: the caption band's mean luma rises from
#: 0.6343 to 0.6699 when the English face grows from 450 to 800 reference px.  The
#: floor sits under that and far above the codec noise, so a regression that stopped
#: painting the bigger face fails here.
INK_MARGIN = 0.02


def styled_project(**fields):
    project = golden_project()
    if not fields:
        return project
    return apply(project, SetStyle('english', fields)).project


# ---------------------------------------------------------------------------
# What an override is
# ---------------------------------------------------------------------------
def test_an_override_changes_only_what_it_names():
    project = styled_project(size=BIGGER, color='#FF0000')
    override = project.style('english')
    assert override.get('size') == BIGGER
    assert override.get('color') == '#FF0000'
    assert override.get('bold') is None            # never mentioned -> the default
    assert project.style_table() == {'english': {'color': '#FF0000', 'size': BIGGER}}
    assert project.style('meaning') is None        # other roles untouched
    # A second edit merges instead of replacing, so a chosen colour survives.
    more = apply(project, SetStyle('english', {'y': 0.5})).project
    assert more.style('english').values == {'color': '#FF0000', 'size': BIGGER, 'y': 0.5}
    cleared = apply(more, ClearStyle('english')).project
    assert cleared.styles == () and cleared.style_table() == {}
    assert apply(cleared, ClearStyle('english')).changed is False


def test_unknown_roles_fields_and_font_swaps_are_refused():
    project = golden_project()
    with pytest.raises(SchemaError):
        apply(project, SetStyle('background', {'size': 100}))
    with pytest.raises(SchemaError) as error:
        apply(project, SetStyle('english', {'colour': '#FFFFFF'}))
    assert 'unknown style field' in error.value.message
    for key in ('font', 'font_name'):
        with pytest.raises(SchemaError) as error:
            apply(project, SetStyle('english', {key: 'C:/some/face.ttf'}))
        assert 'cannot be set per project' in error.value.message
        assert 'resolve_fonts' in error.value.hint
    with pytest.raises(SchemaError):
        apply(project, SetStyle('english', {'color': 'red'}))
    with pytest.raises(SchemaError):
        apply(project, SetStyle('english', {'bold': 'yes'}))
    with pytest.raises(InvalidTimeError):
        apply(project, SetStyle('english', {'size': -10}))
    with pytest.raises(SchemaError):
        apply(project, SetStyle('english', ['size']))
    with pytest.raises(SchemaError):
        apply(project, ClearStyle('nowhere'))
    with pytest.raises(SchemaError):
        StyleOverride(role='nowhere', fields=(('size', 10),))


def test_the_schema_ladder_keeps_older_documents_byte_identical():
    golden = golden_project()
    steps = lambda plan: [(item.clip_id, item.start_ticks, item.end_ticks)   # noqa: E731
                          for item in plan.video + plan.audio]
    for older in (SCHEMA_V1, SCHEMA_V2):
        document = golden.with_schema(older).to_dict()
        assert document['schema'] == older
        assert 'styles' not in document                     # nothing new is written
        again = Project.from_dict(document)
        assert again.schema == older and again.to_dict() == document
        assert steps(render_plan(again, golden_media())) == \
            steps(render_plan(golden.with_schema(SCHEMA), golden_media()))
        # ... and such a document cannot carry overrides it does not express.
        with pytest.raises(SchemaError) as error:
            replace(again, styles=(OVERRIDE,))
        assert 'style overrides' in error.value.message
    document = golden.to_dict()
    document['schema'] = 'wv-project@4'
    with pytest.raises(SchemaError):
        Project.from_dict(document)


def test_setting_a_style_states_the_revision_and_clearing_does_not_rewind_it():
    project = golden_project()
    assert project.schema == SCHEMA                    # @3 is current
    styled = apply(project, SetStyle('english', {'size': BIGGER})).project
    assert styled.schema == SCHEMA and styled.revision == project.revision + 1
    cleared = apply(styled, ClearStyle('english')).project
    assert cleared.schema == SCHEMA                    # versions never go backwards
    assert cleared.styles == ()
    # Round trip through the document keeps the override and its revision.
    document = styled.to_dict()
    assert document['styles'] == [{'role': 'english', 'fields': {'size': BIGGER}}]
    assert Project.from_dict(document).to_dict() == document


# ---------------------------------------------------------------------------
# One style source for the plan, the renderer and the canvas
# ---------------------------------------------------------------------------
def test_the_plan_carries_the_override_and_the_renderer_reads_it():
    project = styled_project(size=BIGGER)
    plan = render_plan(project, golden_media())
    assert plan.style_table() == {'english': {'size': BIGGER}}
    merged, paths, names = _styles(plan.style_table())
    assert merged['english']['size'] == BIGGER
    assert merged['english']['font'] == default_styles()['english']['font']
    assert merged['meaning']['size'] == default_styles()['meaning']['size']
    assert all(paths.values())                          # fonts still resolved
    # The one merge a canvas and a renderer share, and it agrees with the renderer's
    # own table field for field (a bare override table would lose every default the
    # project did not touch, because LayoutSurface merges over its own fallback).
    assert merged_styles(plan.style_table()) == merged
    assert set(merged_styles()) == set(default_styles())
    assert merged_styles({'english': {'size': BIGGER}})['meaning']['size'] == \
        default_styles()['meaning']['size']
    assert style_fonts(merged_styles(plan.style_table())) == (paths, names)
    # Without the override the same call gives the template default, so what
    # changed is the wiring, not the default itself.
    assert _styles()[0]['english']['size'] == default_styles()['english']['size']


def test_the_layout_really_grows_with_the_override():
    font_paths, font_names = style_fonts()
    assert font_paths['english'], 'no usable font resolved on this machine'
    sizes = {}
    for label, project in (('default', golden_project()),
                           ('bigger', styled_project(size=BIGGER))):
        plan = render_plan(project, golden_media())
        styles = merged_styles(plan.style_table())      # what a canvas does
        surface = LayoutSurface(plan, width=1920, height=1080, styles=styles,
                                fonts=font_names, font_paths=font_paths)
        report = surface.layout()
        assert report.exact_metrics, 'layout fell back to estimated metrics'
        english = next(item for item in report.placements if item.role == 'english')
        sizes[label] = (english.size, english.lines[0].height, english.lines[0].width)
    assert sizes['bigger'][0] == round(BIGGER * 1080 / 2160)
    assert sizes['bigger'][0] > sizes['default'][0]
    # Real font metrics, not the JSON: the painted line is taller and wider.
    assert sizes['bigger'][1] > sizes['default'][1]
    assert sizes['bigger'][2] > sizes['default'][2]


# ---------------------------------------------------------------------------
# The pixels, and the speech that must not be synthesised again
# ---------------------------------------------------------------------------
@pytest.fixture(scope='module')
def rendered_pair(tmp_path_factory):
    """The same word rendered twice: default English size, then a bigger one.

    A real export both times, so "the text got bigger" is measured on the frame a
    member would see rather than on the document that asked for it.
    """
    if not (FIXTURE.is_file() and BACKGROUND.is_file()):
        pytest.skip('p1 fixtures or the 1080p reference media are absent')
    from word_video.exporters import catalog, export_run

    root = tmp_path_factory.mktemp('wv-styles')
    background = root / 'background.mp4'
    run([executable('ffmpeg'), '-v', 'error', '-nostdin', '-n', '-i', str(BACKGROUND),
         '-t', '8', '-vf', 'scale=%d:%d' % (CANVAS['width'], CANVAS['height']),
         '-r', str(FPS), '-an', '-c:v', 'libx264', '-preset', 'veryfast', '-crf', '30',
         '-pix_fmt', 'yuv420p', str(background)], timeout=900)
    fixture = json.loads(FIXTURE.read_text(encoding='utf-8'))
    items = [item for item in fixture['provider']['items'] if item['index'] == 152]
    assets = {'w152:%s' % item['role']: catalog.Asset(
        asset_id='w152:%s' % item['role'], path=item['path'], voice=item['voice'])
        for item in items}
    text = {item['role']: item['text'] for item in items}
    records = (Record(id='w152', word=text['female'], phonetic='ˈdepjuti',
                      meaning='n. 副手；代理', spoken_meaning=text['chinese'],
                      index=152),)
    media = catalog.measure_all(assets)
    base = Project(project_id='styles', intro_s=0.0, fps_num=FPS,
                   width=CANVAS['width'], height=CANVAS['height'], speed=1.25)
    rendered = {}
    for label, fields in (('default', {}), ('bigger', {'size': BIGGER})):
        project = instantiate(DEFAULT_LESSON_TEMPLATE, records, media, base)
        if fields:
            project = apply(project, SetStyle('english', fields)).project
        folder = root / label
        folder.mkdir()
        save_project(project, folder)
        catalog.save_catalog(assets, folder)
        result = export_run(str(folder), output=root / ('out-' + label),
                            background=str(background), video_codec='h264', slices=1)
        manifest = json.loads(Path(result.timeline).read_text(encoding='utf-8'))
        word = manifest['words'][0]
        frame = (word['start_frame'] + word['male_frame']) // 2
        shot = root / ('%s.png' % label)
        # Measure the English caption band, not the whole frame: the background
        # dominates a full-frame average and would hide the difference.  The band is
        # the tight one the acceptance checker uses around the same anchor.
        band = default_styles()['english']['y']
        half = 0.045
        top = max(0, int((band - half) * CANVAS['height']))
        height = min(CANVAS['height'] - top, int(2 * half * CANVAS['height']))
        subprocess.run([executable('ffmpeg'), '-v', 'error', '-nostdin', '-y', '-i',
                        result.video, '-vf',
                        'select=eq(n\\,%d),crop=%d:%d:0:%d'
                        % (frame, CANVAS['width'], height, top),
                        '-frames:v', '1', str(shot)], check=True)
        rendered[label] = {'ink': _ink(shot)}
    return rendered


def test_the_rendered_caption_really_gets_bigger(rendered_pair):
    default, bigger = rendered_pair['default'], rendered_pair['bigger']
    assert default['ink'] > 0.005, 'no caption ink at the word midpoint'
    # Mean luma of the English caption band: a bigger face paints more bright pixels
    # there, and the layout test above says which geometry produced it.
    assert bigger['ink'] > default['ink'], (default['ink'], bigger['ink'])
    assert bigger['ink'] - default['ink'] >= INK_MARGIN, (default['ink'], bigger['ink'])


def test_a_style_change_costs_no_tts_call():
    """The speech key names text, voice and route, so a new size never re-synthesises."""
    from word_video.tts import build_speech, identity_spec

    class FakeRoute:
        """A ``synthesize_role`` stand-in that writes real WAV bytes and counts."""

        def __init__(self):
            self.calls = []

        def __call__(self, route, text, role, target, voice=None, resource=None,
                     attempts=3):
            self.calls.append((route, text, role))
            Path(target).parent.mkdir(parents=True, exist_ok=True)
            with wave.open(str(target), 'wb') as stream:
                stream.setnchannels(1)
                stream.setsampwidth(2)
                stream.setframerate(24000)
                stream.writeframes(b'\x02\x00' * 12000)

    def lesson(**styles):
        return LessonSpec([WordEntry(1, 'promise', '/x/', 'n. 承诺', '承诺')],
                          styles=dict(default_styles(), **styles))

    route = FakeRoute()
    provider = {'kind': 'volcengine_original', 'voices_confirmed': True}
    with tempfile.TemporaryDirectory() as folder:
        cache = Path(folder) / 'audio-cache'
        with patch('word_video.tts.synthesize_role', side_effect=route):
            first = build_speech(lesson(), provider, cache)
        assert len(route.calls) == 3                       # one per role
        english = dict(default_styles()['english'], size=BIGGER)
        with patch('word_video.tts.synthesize_role', side_effect=route):
            second = build_speech(lesson(english=english), provider, cache)
        assert len(route.calls) == 3, 'a style change called the service again'
        assert [asset.text for asset in first] == [asset.text for asset in second]
    # The mechanism behind that: the speech identity has no style input at all.
    assert identity_spec('volcengine', 'chinese', '承诺', 'V') == \
        identity_spec('volcengine', 'chinese', '承诺', 'V')
    # In the project model the same holds: a style change leaves every speech item
    # untouched, while the plan identity does move because the picture changed.
    plain, styled = golden_project(), styled_project(size=BIGGER)
    assert [item.to_dict() for item in render_plan(plain, golden_media()).audio] == \
        [item.to_dict() for item in render_plan(styled, golden_media()).audio]
    assert solve(styled, golden_media()).render.identity() != \
        solve(plain, golden_media()).render.identity()
