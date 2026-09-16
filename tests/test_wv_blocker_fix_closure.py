"""A blocker's fix must really clear it — the loop H0 hit, as a regression test.

The delivery preflight is only worth printing if the way out it names *is* a way out.
What H0 measured was the opposite: ``batch submit`` handed back, as its first "fix",
the very ``import audio --voices … --apply`` command the caller had just run — a command
that returned ``ok: true``, changed nothing in the project, and left the batch exactly
as blocked as before.  An Agent following that advice loops.

So every test here takes one blocker, reads the fix the product itself printed, **does
what that fix says through the product's own commands**, and then asserts the blocker is
gone.  A fix that is only prose is not enough: the tests read the command out of the fix
text, so the text and the behaviour cannot drift apart.

The three cases covered are the ones a real batch hits: no delivery picture
(``BACKGROUND_MISSING``), a template that would drop the member's intro
(``TEMPLATE_INTRO_MISMATCH``), and — the H0 case — recordings with no recorded voice
(``VOICE_UNRECORDED``, a *warning*: it does not stop a delivery, so it must not appear
among the blockers' fixes either).
"""
from dataclasses import replace
import hashlib
import importlib
import json
from pathlib import Path

import pytest

from test_wv_batch_packages import intro_project, plan_arguments, run_cli, source

from word_video.application.templates import TemplateDocument
from word_video.domain import DEFAULT_LESSON_TEMPLATE
from word_video.domain.compile import IntroMeasurement
from word_video.storage import AssetIndex
from word_video.storage.coordinator import Coordinator
from word_video.storage.project_store import load_project
from word_video.storage.templates import TemplateStore

VOICES = 'female=BV503_streaming,male=BV504_streaming,chinese=BV406_streaming'
RECORDED = ('w151:chinese', 'w151:female', 'w151:male', 'w152:chinese', 'w152:female',
            'w152:male', 'w153:chinese', 'w153:female', 'w153:male')


def by_name(checks):
    return {item['name']: item for item in checks['checks']}


def cache_root(folder):
    """The ``audio-cache`` tree this fixture's recordings were written into."""
    ref = AssetIndex.load(folder).ref('w151:female')
    return Path(ref.path).parents[1]


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


@pytest.fixture
def unrecorded(source):
    """The same batch with no voice on any recording — H0's starting state."""
    root, folder = source
    index = AssetIndex.load(folder)
    replace(index, refs=tuple(replace(ref, voice='') for ref in index.refs)).save(folder)
    return root, folder


# ---------------------------------------------------------------------------
# plan and submit are the same verdict
# ---------------------------------------------------------------------------
def test_plan_and_submit_share_one_verdict(unrecorded):
    """``plan.ready``/``blocking`` and a refused submit are the same answer.

    Two independent judgements are what let an Agent read "nothing blocks", run the
    fixes it was handed, and be refused by the same command again.
    """
    root, folder = unrecorded
    result = run_cli(root, *plan_arguments(folder))[1]['result']
    assert result['ready'] is True and result['blocking'] == []
    assert result['checks']['ready'] is True and result['checks']['blocking'] == []
    assert result['warnings'] == ['voices']              # named, not hidden
    # ... and submit agrees, because it reads the same verdict.
    code, submitted, _ = run_cli(root, 'batch', 'submit', '--project', str(folder),
                                 '--batch', '151-153', '--key', 'same-verdict')
    assert code == 0 and submitted['result']['job']

    # Now make it unproducible: the picture is gone, and both commands say so.
    (folder / 'delivery.json').unlink()
    planned = run_cli(root, *plan_arguments(folder))[1]['result']
    refused = run_cli(root, 'batch', 'submit', '--project', str(folder),
                      '--batch', '151-153', '--key', 'no-picture')
    assert planned['ready'] is False and 'BACKGROUND_MISSING' in planned['blocking']
    assert planned['checks']['ready'] is False
    assert planned['checks']['blocking'] == planned['blocking']
    assert refused[0] == 2 and refused[1]['error']['code'] == 'NEEDS_INPUT'
    assert '1 problem(s)' in refused[1]['error']['message']


# ---------------------------------------------------------------------------
# The voices warning: its fix records the voices, and it is not mixed into `fixes`
# ---------------------------------------------------------------------------
def test_the_voice_warning_names_a_command_that_really_records_the_voices(unrecorded):
    root, folder = unrecorded
    project_file = folder / 'assets.json'
    before = json.loads(project_file.read_text(encoding='utf-8'))
    assert not any(ref.get('voice') for ref in before['assets'])

    result = run_cli(root, *plan_arguments(folder))[1]['result']
    voices = by_name(result['checks'])['voices']
    assert voices['ok'] is False and voices['blocking'] is False
    assert voices['unrecorded'] and voices['recorded'] == 0
    # A warning: the delivery is producible, so it does not block — and it is not in the
    # blockers' fixes either, which is where H0's loop started.  The plan still hands
    # the caller the command that records the voices.
    assert 'voices' in result['warnings'] and result['blocking'] == []
    assert result['fixes'] == []
    fix = ' '.join(result['warning_fixes'])
    assert 'import audio' in fix and '--apply' in fix and 'assets.json' in fix

    # Do exactly what the fix says: import the recordings the project already has and
    # name the voice of each.  This is the command H0 ran and got `ok: true` from.
    code, imported, _ = run_cli(root, 'import', 'audio', '--project', str(folder),
                                '--roots', str(cache_root(folder)), '--batch', '151-153',
                                '--voices', VOICES, '--apply')
    assert code == 0, imported
    recorded = imported['result']['voices']
    assert recorded['unmatched'] == []
    assert tuple(sorted(recorded['written'])) == RECORDED
    assert recorded['saved'] is True

    # The blocker really is gone: every asset the delivery reuses can name its voice.
    after = json.loads(project_file.read_text(encoding='utf-8'))
    assert all(ref.get('voice') for ref in after['assets'])
    assert {ref['asset_id']: ref['voice'] for ref in after['assets']}['w151:female'] \
        == 'BV503_streaming'
    voices = by_name(run_cli(root, *plan_arguments(folder))[1]['result']['checks'])['voices']
    assert voices['ok'] is True and voices['unrecorded'] == []
    assert voices['recorded'] == 9


def test_importing_the_same_recordings_twice_leaves_the_registry_byte_for_byte(
        unrecorded):
    """Idempotent: the second import writes nothing at all, not "the same values"."""
    root, folder = unrecorded
    assets = folder / 'assets.json'
    argv = ('import', 'audio', '--project', str(folder), '--roots',
            str(cache_root(folder)), '--batch', '151-153', '--voices', VOICES, '--apply')
    assert run_cli(root, *argv)[0] == 0
    first = digest(assets)
    code, again, _ = run_cli(root, *argv)
    assert code == 0, again
    assert again['result']['voices']['saved'] is False
    assert again['result']['voices']['written'] == []
    assert digest(assets) == first
    # The published originals are reused too: a second import does not grow the disk.
    assert again['result']['report']['published_new'] == 0
    assert again['result']['report']['reused'] == 9


def test_a_recording_that_matches_no_asset_is_named_not_skipped(source):
    """The other silent-failure mode: success reported while nothing was recorded."""
    root, folder = source
    cache = cache_root(folder)
    index = AssetIndex.load(folder)
    # Rename the ids the *registry* uses; the project's clips still name the old ones,
    # so the import finds the recordings and cannot attach them to anything.
    AssetIndex.of(tuple(replace(ref, asset_id='renamed-%s' % ref.asset_id)
                        for ref in index.refs), project_id=index.project_id,
                  folder=str(folder)).save(folder)
    code, document, _ = run_cli(root, 'import', 'audio', '--project', str(folder),
                                '--roots', str(cache), '--batch', '151-153',
                                '--voices', VOICES, '--apply')
    assert code == 2 and document['error']['code'] == 'NEEDS_INPUT'
    assert 'w151:female' in ' '.join(document['error']['fixes'])
    # Nothing was half-written: the registry is exactly what it was.
    saved = json.loads((folder / 'assets.json').read_text(encoding='utf-8'))
    assert all(ref['asset_id'].startswith('renamed-') for ref in saved['assets'])


# ---------------------------------------------------------------------------
# The background: the fix is the flag the refusal names
# ---------------------------------------------------------------------------
def test_the_background_fix_really_clears_the_refusal(source):
    root, folder = source
    (folder / 'delivery.json').unlink()                # nothing sets a picture
    picture = folder / 'background.mp4'
    assert picture.is_file()

    result = run_cli(root, *plan_arguments(folder))[1]['result']
    assert result['ready'] is False
    assert result['blocking'] == ['BACKGROUND_MISSING']
    assert result['checks']['blocking'] == ['BACKGROUND_MISSING']
    assert result['plan']['delivery']['ready'] is False
    assert any('--background' in fix for fix in result['fixes'])
    assert any('--background' in fix for fix in result['delivery_fixes'])
    # The advice of a warning is not a fix for this refusal.
    assert not any('import audio' in fix for fix in result['fixes'])

    # Do exactly what the fix says, and the refusal is gone.
    code, submitted, _ = run_cli(root, 'batch', 'submit', '--project', str(folder),
                                 '--batch', '151-153', '--key', 'with-picture',
                                 '--background', str(picture))
    assert code == 0, submitted
    assert submitted['result']['job']
    planned = run_cli(root, *plan_arguments(folder, '--background',
                                            str(picture)))[1]['result']
    assert planned['ready'] is True and planned['blocking'] == []
    assert planned['plan']['delivery']['ready'] is True


# ---------------------------------------------------------------------------
# The arrangement: the reference layout keeps the member's intro
# ---------------------------------------------------------------------------
def intro_measure():
    return IntroMeasurement(asset_id='layer.intro.mov', seconds=1.0, sound_asset='',
                            sound_source='silent', from_clip=False, picture_seconds=1.0)


def _with_intro(folder):
    """Give the fixture's project an intro layer of its own.

    ``intro_project`` (from the batch-package tests) writes the layer with a *stub*
    measurement: measuring real media is the IO edge, while what is under test here is
    which arrangement a batch keeps.
    """
    records = tuple(load_project(folder).records)
    intro_project(folder, records, AssetIndex.load(folder).media_map(), seconds=1.0)
    return folder


def _stub_intro_measurement(monkeypatch):
    cli_module = importlib.import_module('word_video.cli.main')
    monkeypatch.setattr(cli_module, 'measure_project_intro',
                        lambda *args, **kwargs: intro_measure())


def test_the_reference_layout_keeps_the_intro_and_never_drops_it(source, monkeypatch):
    """Nothing pinned: the layer the member placed is kept, so nothing has to be fixed.

    This is the second blocker H0's chain hit and did not name: with the reference
    template the plan refused a project that has an intro ("this batch will not draw the
    intro"), so a real project was un-submittable until someone recorded the arrangement
    first.  The reference layout is the layout the verified engine draws, and that
    engine draws the project's own countdown.
    """
    root, folder = source
    _with_intro(folder)
    _stub_intro_measurement(monkeypatch)

    result = run_cli(root, *plan_arguments(folder))[1]['result']
    assert result['ready'] is True and result['blocking'] == []
    arrangement = by_name(result['checks'])['template']
    assert arrangement['intro_in_source'] is True
    assert arrangement['intro_in_template'] is False
    assert arrangement['intro_from'] == 'source' and arrangement['intro_used'] is True

    # And the instance really carries the layer: "ready" is not a promise.
    code, submitted, _ = run_cli(root, 'batch', 'submit', '--project', str(folder),
                                 '--batch', '151-153', '--key', 'keeps-intro')
    assert code == 0, submitted
    instance = Coordinator(root).load_project(
        submitted['result']['packages'][0]['project_id'])
    assert instance.intro_clip() is not None


def test_a_stored_batch_document_written_before_the_verdict_still_reads_blocked():
    """Reading an older ``wv-batch@1`` document must not turn blocked into ready.

    The batch's own problems were always stored on its packages, so the one verdict
    rebuilds from them: a document saved by the previous build (no ``blockers`` key at
    all) reports the same blocker, and therefore the same refusal, as the day it was
    planned.
    """
    from word_video.application.packages import BatchDocument

    loaded = BatchDocument.from_dict({
        'schema': 'wv-batch@1', 'batch_id': 'b151-153-x1-deadbeef',
        'profile': {'background': '', 'video_codec': 'h264', 'slices': None},
        'selection': {'first': 151, 'last': 153}, 'rule': {'per_package': 0, 'count': 0},
        'packages': [{'package_id': 'p151-153', 'project_id': 'b1-p151-153',
                      'record_ids': ['w151'], 'words': [], 'problems': [
                          {'code': 'TEMPLATE_INTRO_MISMATCH', 'message': '模板没有片头图层',
                           'object_path': 'template', 'hint': '用 template save '
                                                             '--from-project 记录含片头的模板'}],
                      'delivery': {'ready': True, 'background': 'b.mp4', 'problems': [],
                                   'fixes': []}}],
        # The previous shape: an aggregate computed over the areas alone, which is
        # exactly what used to disagree with a submit.
        'checks': {'ready': True, 'blocking': [], 'checks': [], 'fixes': []}})
    assert loaded.readiness().names() == ('TEMPLATE_INTRO_MISMATCH',)
    assert loaded.ready is False
    assert loaded.checks.ready is False and loaded.checks.blocking == \
        ('TEMPLATE_INTRO_MISMATCH',)
    assert any('template save --from-project' in fix for fix in loaded.fixes())


def test_a_pinned_template_that_would_drop_the_intro_is_refused_and_its_fix_clears_it(
        source, monkeypatch):
    """Pinning is a choice: a template that disagrees with the source is still refused.

    The refusal is not lowered — it is made *actionable*: the fix is the capture command
    ``template save --from-project``, and running it, as an Agent would, turns the
    refusal into a ready batch that really carries the layer.
    """
    root, folder = source
    _with_intro(folder)
    _stub_intro_measurement(monkeypatch)
    # A stored template that declares no intro layer, pinned by name.
    TemplateStore(root).save(TemplateDocument(template_id='no-intro', version=1,
                                              lesson=DEFAULT_LESSON_TEMPLATE,
                                              note='不含片头的版式'))

    result = run_cli(root, *plan_arguments(folder, '--template', 'no-intro'))[1]['result']
    assert result['ready'] is False
    assert result['blocking'] == ['TEMPLATE_INTRO_MISMATCH']
    assert 'TEMPLATE_INTRO_MISMATCH' in [problem['code']
                                         for problem in result['plan']['problems']]
    assert any('template save --from-project' in fix for fix in result['fixes'])

    # The fix, exactly as printed: capture the arrangement the member actually has.
    code, saved, _ = run_cli(root, 'template', 'save', '--from-project', str(folder),
                             '--template', 'p1-lesson')
    assert code == 0, saved
    assert saved['result']['template']['intro'] is True
    planned = run_cli(root, *plan_arguments(folder, '--template', 'p1-lesson',
                                            '--template-version', '1'))[1]['result']
    assert planned['ready'] is True and planned['blocking'] == []
    assert by_name(planned['checks'])['template']['intro_from'] == 'template'
    code, submitted, _ = run_cli(root, 'batch', 'submit', '--project', str(folder),
                                 '--batch', '151-153', '--key', 'captured',
                                 '--template', 'p1-lesson')
    assert code == 0, submitted
    instance = Coordinator(root).load_project(
        submitted['result']['packages'][0]['project_id'])
    assert instance.intro_clip() is not None
