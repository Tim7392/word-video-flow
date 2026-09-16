"""One writer, frozen submissions, receipts, and what a killed worker leaves behind.

The six things this file has to prove are the W05 acceptance list: two clients
cannot both win a save; the same key with the same content is the same task while
different content conflicts; a retry of a finished task executes nothing; a changed
input file stops the old task instead of silently rendering something else;
cancelling publishes nothing; and a worker that was killed is reconciled to
``interrupted`` — never to success, and never with half a delivery counted as one.

A fake runner stands in for the renderer: the acceptance is about coordination, and
the real one is already covered by B's exporter tests.  It writes a video, a SRT and
a draft name into the **run folder** — the folder the run is published from, which
is what keeps the absolute paths a real exporter writes inside its own documents
(``complete.json``, ``timeline.json``) valid after the run is published.

A delivery also needs a background file (H0's review): the profile check that
refuses one without it is here, because "no picture" used to reach the exporter as
an empty path, i.e. the current working directory, and came back as
``PermissionError`` on a folder.
"""
from dataclasses import replace
import json
from pathlib import Path

import pytest

from test_wv_project_golden import golden_base, golden_media, golden_project, golden_record

from word_video.application import MoveClip, apply, instantiate
from word_video.application.batches import (BatchSelection, ExportProfile,
                                           check_delivery, plan_batch, submission_for)
from word_video.domain import DEFAULT_LESSON_TEMPLATE, StaleRevisionError
from word_video.storage import AssetIndex, AssetRef, save_project
from word_video.storage.coordinator import (RECOVERY_DIR, RUNS_DIR, Coordinator,
                                            IdempotencyConflict, InputChanged,
                                            JobStateError, NeedsInput,
                                            cleanup_partial, connect, fingerprint)


def build_project(folder, *, revision=0):
    """A golden lesson saved under ``folder`` with a registry beside it."""
    project = instantiate(DEFAULT_LESSON_TEMPLATE, (golden_record(),), golden_media(),
                          replace(golden_base(), revision=revision))
    folder.mkdir(parents=True, exist_ok=True)
    save_project(project, folder)
    index = AssetIndex.of(
        (AssetRef('w1:female', 'media/female.wav', units=55200),
         AssetRef('w1:male', 'media/male.wav', units=40800),
         AssetRef('w1:chinese', 'media/chinese.wav', units=157200)),
        project_id=project.project_id, folder=str(folder))
    for name in ('female', 'male', 'chinese'):
        media = folder / 'media' / ('%s.wav' % name)
        media.parent.mkdir(parents=True, exist_ok=True)
        media.write_bytes(b'x' * 32)
    index.save(folder)
    background_file(folder)
    return project, index


def background_file(folder):
    """The delivery background a batch needs: a file, not project content."""
    path = folder / 'background.mp4'
    path.write_bytes(b'not really a video, but a readable file')
    return path


def _background(root, project_id='golden'):
    return root / 'projects' / project_id / 'background.mp4'


def fake_runner(submission, run_dir, checkpoint):
    """Stand in for the real export: three artifacts, correct shape, no ffmpeg.

    It writes the paths a real run carries into ``timeline.json``/``complete.json``
    — absolute, pointing inside its own folder — so a publication step that moved
    the folder afterwards would leave them dangling (the defect this file pins).
    """
    run_dir = Path(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / 'video').mkdir(exist_ok=True)
    (run_dir / 'srt').mkdir(exist_ok=True)
    (run_dir / 'editable-draft').mkdir(exist_ok=True)
    (run_dir / 'video' / 'video.mp4').write_bytes(b'video')
    (run_dir / 'background.mp4').write_bytes(b'looped background, as a split one is')
    (run_dir / 'srt' / '0151-0153_01_英文重复.srt').write_text('1\n', encoding='utf-8')
    (run_dir / 'editable-draft' / 'draft_content.json').write_text(
        json.dumps({'references': [str(run_dir / 'video' / 'video.mp4')]}),
        encoding='utf-8')
    (run_dir / 'timeline.json').write_text(
        json.dumps({'background': str(run_dir / 'background.mp4')}), encoding='utf-8')
    checkpoint()
    recorded = run_dir / 'complete.json'
    recorded.write_text(json.dumps({'files': [
        {'path': str(path)} for path in sorted(run_dir.rglob('*'))
        if path.is_file()]}), encoding='utf-8')
    return {'run_dir': str(run_dir)}


@pytest.fixture
def root(tmp_path):
    work = tmp_path / 'root'
    work.mkdir()
    return work


@pytest.fixture
def coordinator(root):
    return Coordinator(root, runner=fake_runner)


def submission_for_project(root, project_id='golden'):
    folder = root / 'projects' / project_id
    project = Coordinator(root).load_project(project_id)
    index = AssetIndex.load(folder)
    background = folder / 'background.mp4'
    plan = plan_batch(project, index.media_map(), BatchSelection(),
                      ExportProfile(background=str(background)))
    return submission_for(plan, (str(folder / 'project.json'),
                                str(folder / 'assets.json'), str(background)))


# ---------------------------------------------------------------------------
# 1. Two clients, one writer
# ---------------------------------------------------------------------------
def test_two_clients_saving_the_same_project_cannot_both_win(coordinator, root):
    build_project(root / 'projects' / 'golden')
    base = coordinator.load_project('golden')
    mine = apply(base, MoveClip('w1.male', 144000)).project       # revision 1
    theirs = apply(base, MoveClip('w1.female', 24000)).project    # also revision 1
    saved = coordinator.save_project(mine, expected_revision=base.revision)
    assert saved.is_file()
    assert coordinator.project_revision('golden') == 1
    with pytest.raises(StaleRevisionError) as error:
        coordinator.save_project(theirs, expected_revision=base.revision)
    assert error.value.code == 'STALE_REVISION'
    assert 'not 0' in error.value.message          # names both revisions
    # The winner's content is intact and the loser changed nothing on disk.
    loaded = coordinator.load_project('golden')
    assert loaded.clip('w1.male').start.ticks == 1440000 + 144000
    assert loaded.clip('w1.female').start.ticks == 720000
    # The replaced revision is kept beside the project, so a bad save is recoverable.
    backups = sorted(path.name for path in
                     (root / 'projects' / 'golden' / 'backups').iterdir())
    assert backups == ['rev0.json']
    assert json.loads((root / 'projects' / 'golden' / 'backups' / 'rev0.json')
                      .read_text(encoding='utf-8'))['revision'] == 0


def test_saving_without_an_expectation_still_advances_the_index(coordinator, root):
    build_project(root / 'projects' / 'golden')
    base = coordinator.load_project('golden')
    coordinator.save_project(apply(base, MoveClip('w1.male', 12000)).project)
    assert coordinator.project_revision('golden') == 1
    assert coordinator.project_index()[0]['project_id'] == 'golden'


# ---------------------------------------------------------------------------
# 2. Idempotency and receipts
# ---------------------------------------------------------------------------
def test_the_same_key_with_the_same_content_is_the_same_task(coordinator, root):
    build_project(root / 'projects' / 'golden')
    submission = submission_for_project(root)
    first = coordinator.submit(submission, 'batch-151-153')
    assert first['created'] is True
    again = coordinator.submit(submission, 'batch-151-153')
    assert again['created'] is False
    assert again['job'] == first['job']            # a retry never spends twice
    rows = coordinator.receipts()
    assert len(rows) == 1
    assert rows[0]['idempotency_key'] == 'batch-151-153'
    assert rows[0]['job_id'] == first['job']
    assert rows[0]['plan_identity'] == submission.plan.plan_identity
    assert json.loads(rows[0]['inputs'])[0]['sha256']


def test_the_same_key_with_different_content_is_a_conflict(coordinator, root):
    build_project(root / 'projects' / 'golden')
    submission = submission_for_project(root)
    coordinator.submit(submission, 'batch-151-153')
    changed = submission_for_project(root)
    changed = replace(changed, plan=replace(
        changed.plan, profile=ExportProfile(background=str(_background(root)),
                                            video_codec='h265')))
    with pytest.raises(IdempotencyConflict) as error:
        coordinator.submit(changed, 'batch-151-153')
    assert error.value.code == 'IDEMPOTENCY_CONFLICT'
    assert 'different request' in error.value.message
    # A new key is accepted, and both receipts stay checkable in the same database.
    other = coordinator.submit(changed, 'batch-151-153-h265')
    assert other['created'] is True
    assert len(coordinator.receipts()) == 2


def test_submit_needs_a_key(coordinator, root):
    build_project(root / 'projects' / 'golden')
    with pytest.raises(NeedsInput) as error:
        coordinator.submit(submission_for_project(root), '   ')
    assert error.value.code == 'NEEDS_INPUT'
    assert error.value.fixes


def test_retrying_a_finished_task_does_nothing(coordinator, root):
    build_project(root / 'projects' / 'golden')
    job = coordinator.submit(submission_for_project(root), 'k')['job']
    finished = coordinator.run(job)
    assert finished['state'] == 'succeeded'
    published = [item['path'] for item in finished['artifacts']]
    assert published and all(Path(path).is_file() for path in published)
    before = sorted(str(path.relative_to(root)) for path in root.rglob('*'))
    again = coordinator.run(job)
    assert again['state'] == 'succeeded'
    assert [item['path'] for item in again['artifacts']] == published
    assert sorted(str(path.relative_to(root)) for path in root.rglob('*')) == before


def test_a_published_run_keeps_the_paths_it_wrote_inside_its_own_folder(coordinator, root):
    """The defect H0 caught: a delivery whose internal paths were left dangling.

    A real exporter writes absolute paths into ``complete.json``/``timeline.json``/
    the draft's resource references.  Publishing by renaming a staging folder leaves
    every one of them pointing at a folder that no longer exists, so the run must be
    exported where it will be published.
    """
    build_project(root / 'projects' / 'golden')
    job = coordinator.submit(submission_for_project(root), 'k')['job']
    finished = coordinator.run(job)
    assert finished['state'] == 'succeeded'
    run_dir = root / RUNS_DIR / job
    assert run_dir.is_dir() and 'staging' not in str(run_dir)
    assert not (root / 'staging').exists()
    # Every path the run's own documents carry still resolves.
    for name in ('complete.json', 'timeline.json',
                 'editable-draft/draft_content.json'):
        document = json.loads((run_dir / name).read_text(encoding='utf-8'))
        paths = [item['path'] for item in document.get('files', [])] \
            or document.get('references', []) or [document['background']]
        assert paths, name
        missing = [path for path in paths if not Path(path).exists()]
        assert not missing, (name, missing)
        assert all(str(run_dir) in path for path in paths), (name, paths)
    # And the recorded artifacts are the same files, at the same place.
    assert {item['path'] for item in finished['artifacts']} == \
        {str(path) for path in run_dir.rglob('*') if path.is_file()}


# ---------------------------------------------------------------------------
# 3. Frozen inputs
# ---------------------------------------------------------------------------
def test_changing_an_input_stops_the_old_task(coordinator, root):
    build_project(root / 'projects' / 'golden')
    job = coordinator.submit(submission_for_project(root), 'k')['job']
    (root / 'projects' / 'golden' / 'assets.json').write_text('{}', encoding='utf-8')
    with pytest.raises(InputChanged) as error:
        coordinator.run(job)
    assert error.value.code == 'INPUT_CHANGED'
    assert 'input changed' in error.value.message
    assert coordinator.status(job)['state'] == 'failed'
    assert coordinator.status(job)['artifacts'] == []


def test_a_new_submission_after_the_change_runs(coordinator, root):
    build_project(root / 'projects' / 'golden')
    first = coordinator.submit(submission_for_project(root), 'k1')['job']
    (root / 'projects' / 'golden' / 'assets.json').write_text(
        (root / 'projects' / 'golden' / 'assets.json').read_text(encoding='utf-8'),
        encoding='utf-8')
    second = coordinator.submit(submission_for_project(root), 'k2')['job']
    assert second != first
    assert coordinator.run(second)['state'] == 'succeeded'


# ---------------------------------------------------------------------------
# 4. Cancel publishes nothing; a killed worker is reconciled
# ---------------------------------------------------------------------------
def test_cancelling_before_the_run_publishes_nothing(coordinator, root):
    build_project(root / 'projects' / 'golden')
    job = coordinator.submit(submission_for_project(root), 'k')['job']
    cancelled = coordinator.cancel(job)
    assert cancelled['state'] == 'cancelled'
    assert coordinator.run(job)['state'] == 'cancelled'
    assert coordinator.artifacts(job)['published'] is False
    assert not (root / 'runs' / job).exists()
    with pytest.raises(JobStateError):
        coordinator.cancel(job) if False else coordinator.resume(job) if False else \
            coordinator.control(job, 'nonsense')


def test_cancelling_during_the_run_recovers_the_unfinished_run(coordinator, root):
    build_project(root / 'projects' / 'golden')
    job = coordinator.submit(submission_for_project(root), 'k')['job']

    def cancelling_runner(submission, run_dir, checkpoint):
        fake_runner(submission, run_dir, lambda: None)
        coordinator.cancel(job)                    # asked for while it was rendering
        checkpoint()                               # stops the run here
        return {}

    coordinator.runner = cancelling_runner
    stopped = coordinator.run(job)
    assert stopped['state'] == 'cancelled'
    assert not (root / RUNS_DIR / job).exists()
    assert coordinator.artifacts(job)['published'] is False
    assert list((root / RECOVERY_DIR).iterdir())   # kept as evidence, not published


def test_a_killed_worker_is_reconciled_to_interrupted(coordinator, root):
    build_project(root / 'projects' / 'golden')
    job = coordinator.submit(submission_for_project(root), 'k')['job']
    # A worker that died between "running" and any publish: the row says running,
    # the lock is free, and its run folder holds a half-written export.
    run_dir = root / RUNS_DIR / job
    (run_dir / 'video').mkdir(parents=True, exist_ok=True)
    (run_dir / 'video' / 'video.mp4').write_bytes(b'half')
    with connect(coordinator.db) as con:
        con.execute("UPDATE jobs SET state='running', phase='rendering' WHERE id=?",
                    (job,))
    report = coordinator.reconcile()
    assert job in report['interrupted']
    assert coordinator.status(job)['state'] == 'interrupted'
    assert coordinator.artifacts(job)['published'] is False
    # ``runs/`` holds deliveries: cleanup moves the unfinished folder to recovery.
    moved = cleanup_partial(root)
    assert moved and not run_dir.exists()
    assert list((root / RECOVERY_DIR).iterdir())
    assert coordinator.cleanup() == []              # idempotent: nothing left behind


def test_cleanup_keeps_a_published_run_and_moves_a_stranger(coordinator, root):
    build_project(root / 'projects' / 'golden')
    job = coordinator.submit(submission_for_project(root), 'k')['job']
    assert coordinator.run(job)['state'] == 'succeeded'
    stranger = root / RUNS_DIR / 'wv-not-in-the-database'
    (stranger / 'video').mkdir(parents=True)
    (stranger / 'video' / 'video.mp4').write_bytes(b'x')
    moved = coordinator.cleanup()
    assert (root / RUNS_DIR / job).is_dir()          # a delivery stays where it is
    assert not stranger.exists()                     # nothing can be shown to be one
    assert len(moved) == 1


# ---------------------------------------------------------------------------
# 4b. A delivery without a picture is refused by name, not by cwd
# ---------------------------------------------------------------------------
def test_a_batch_without_a_background_is_refused_with_fixes(coordinator, root):
    build_project(root / 'projects' / 'golden')
    folder = root / 'projects' / 'golden'
    project = coordinator.load_project('golden')
    index = AssetIndex.load(folder)
    plan = plan_batch(project, index.media_map(), BatchSelection(), ExportProfile())
    assert plan.ok                                    # the lesson solves fine
    assert plan.ready is False
    assert [problem.code for problem in plan.delivery.problems] == \
        ['BACKGROUND_MISSING']
    assert plan.delivery.fixes and '--background' in ' '.join(plan.delivery.fixes)

    with pytest.raises(NeedsInput) as error:
        coordinator.submit(submission_for(plan, ()), 'k')
    assert error.value.code == 'NEEDS_INPUT'
    assert '--background' in ' '.join(error.value.fixes)

    # A background path that does not exist is named too, rather than probed as cwd.
    missing = plan_batch(project, index.media_map(), BatchSelection(),
                         ExportProfile(background=str(folder / 'ghost.mp4')))
    assert [problem.code for problem in missing.delivery.problems] == \
        ['BACKGROUND_FILE_MISSING']
    assert str(folder / 'ghost.mp4') in missing.delivery.problems[0].message

    # And the deliveries it *can* accept are the ones with a readable file.
    assert check_delivery(ExportProfile(
        background=str(background_file(folder)))).ready is True


def test_a_submission_frozen_before_the_delivery_check_is_refused_at_run(coordinator,
                                                                        root):
    """The backstop H0 asked for: no INTERNAL, no PermissionError on the cwd."""
    build_project(root / 'projects' / 'golden')
    job = coordinator.submit(submission_for_project(root), 'k')['job']
    with connect(coordinator.db) as con:
        row = con.execute('SELECT submission FROM jobs WHERE id=?', (job,)).fetchone()
        document = json.loads(row['submission'])
        document['plan'].pop('delivery')              # as an older build froze it
        con.execute('UPDATE jobs SET submission=? WHERE id=?',
                    (json.dumps(document), job))
    with pytest.raises(NeedsInput) as error:
        coordinator.run(job)
    assert '--background' in ' '.join(error.value.fixes)
    assert not (root / RUNS_DIR / job).exists()


def test_a_published_task_whose_artifacts_vanish_is_not_success(coordinator, root):
    build_project(root / 'projects' / 'golden')
    job = coordinator.submit(submission_for_project(root), 'k')['job']
    finished = coordinator.run(job)
    Path(finished['artifacts'][0]['path']).unlink()
    report = coordinator.reconcile()
    assert job in report['partial_failed']
    assert coordinator.status(job)['state'] == 'partial_failed'
    assert 'changed after publish' in coordinator.status(job)['error']


# ---------------------------------------------------------------------------
# 5. Planning writes nothing
# ---------------------------------------------------------------------------
def test_planning_reports_problems_and_writes_no_file(coordinator, root):
    build_project(root / 'projects' / 'golden')
    folder = root / 'projects' / 'golden'
    before = sorted(str(path.relative_to(root)) for path in root.rglob('*'))
    project = coordinator.load_project('golden')
    index = AssetIndex.load(folder)
    plan = plan_batch(project, index.media_map(), BatchSelection(record_ids=('w1',)),
                      ExportProfile())
    assert plan.ok and plan.plan_identity and plan.total_ticks == 3960000
    assert plan.records[0].id == 'w1'
    assert sorted(str(path.relative_to(root)) for path in root.rglob('*')) == before

    empty = plan_batch(project, {}, BatchSelection(), ExportProfile())
    assert not empty.ok
    assert {problem.code for problem in empty.problems} == {'UNKNOWN_DURATION'}
    assert empty.problems[0].hint and empty.problems[0].path.startswith('clip:')
    assert '未测量' in empty.problems[0].message or 'has not been measured' in \
        empty.problems[0].message


def test_the_coordinator_database_holds_tasks_receipts_and_artifacts(coordinator, root):
    build_project(root / 'projects' / 'golden')
    job = coordinator.submit(submission_for_project(root), 'k')['job']
    coordinator.run(job)
    with connect(coordinator.db) as con:
        tables = sorted(row['name'] for row in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"))
        jobs = con.execute('SELECT state, phase, control FROM jobs').fetchall()
        artifacts = con.execute('SELECT COUNT(*) AS n FROM artifacts').fetchone()['n']
    assert set(tables) - {'sqlite_sequence'} == {'artifacts', 'jobs', 'projects',
                                                 'receipts'}
    assert [(row['state'], row['phase'], row['control']) for row in jobs] == \
        [('succeeded', 'done', 'run')]
    assert artifacts == 6            # video, background, srt, draft, timeline, complete
    # The project itself is *not* duplicated into the database: project.json stays
    # the one editable truth.
    assert json.loads((root / 'projects' / 'golden' / 'project.json')
                      .read_text(encoding='utf-8'))['schema'].startswith('wv-project@')


def test_fingerprints_ignore_files_that_are_not_there(root):
    present = root / 'a.bin'
    present.write_bytes(b'x')
    prints = fingerprint([str(present), str(root / 'ghost.bin')])
    assert [item.path for item in prints] == [str(present)]
    assert len(prints[0].sha256) == 64
