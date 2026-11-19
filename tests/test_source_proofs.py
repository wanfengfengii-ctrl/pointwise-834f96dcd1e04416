from contextlib import closing
from dataclasses import replace
import json
import sqlite3

from archive import (
    StoredEvidenceError, export_source_segment, get, seal, verify_source_segment,
)
from database import open_database
from evidence.frames import FrameLedger, RawFrame
from evidence.intervals import Interval
from evidence.receipts import SourceReceipt
from evidence.source_proofs import SourceAnchor
from registry import initialize_defaults
from samples.portal_fixture import STATION
from samples.source_proofs import (
    CONDITION, END, START, SOURCE_ROWS, ReplayPortal, queue_frames,
)
from sessions.driver import step
from sessions.opening import start
from sessions.runs import RunStatus, RunStore
from tests import ShiftCase


class SourceProofCase(ShiftCase):
    def setUp(self):
        super().setUp()
        self.portal.shutdown()
        self.portal = ReplayPortal()
        self.addCleanup(self.portal.shutdown)

    def open_source_run(self):
        run_id = start(
            self.db, self.portal, STATION, self.default_id, CONDITION, START, source_end=END,
        )
        handle, = self.portal.active_handles
        queue_frames(self.portal, handle)
        return run_id, handle

    def emit_claim(self, handle):
        delivery = self.portal.emit_source_receipt(handle, START, END)
        anchor = self.portal.source_anchor(handle, START, END)
        return delivery, anchor


class TestSourceOpening(SourceProofCase):
    def test_source_end_binding(self):
        run_id, _ = self.open_source_run()
        run = RunStore(self.db).load(run_id)
        self.assertEqual((run.start_seq, run.source_end), (START, END))

    def test_invalid_source_end(self):
        for kwargs in ({'source_end': START}, {'source_end': START - 1}):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(ValueError):
                    start(self.db, self.portal, STATION, self.default_id, CONDITION, START, **kwargs)
        self.assertEqual(self.portal.active_handles, ())
        self.assertEqual(self.db.execute('SELECT count(*) FROM runs').fetchone()[0], 0)

    def test_start_before_eligibility_rejected(self):
        # A late registration makes the first eligible sequence exceed the start.
        self.portal.advance(('salinity', 1))
        with self.assertRaisesRegex(ValueError, 'observation eligibility'):
            start(self.db, self.portal, STATION, self.default_id, CONDITION, START, source_end=END)
        self.assertEqual(self.portal.active_handles, ())

    def test_omitted_source_end_keeps_v1(self):
        run_id = start(self.db, self.portal, STATION, self.default_id, CONDITION, START)
        self.assertIsNone(RunStore(self.db).load(run_id).source_end)


class TestSourceAdmission(SourceProofCase):
    def test_complete_receipt_admitted(self):
        run_id, handle = self.open_source_run()
        _, anchor = self.emit_claim(handle)
        report = step(self.db, self.portal, run_id)
        self.assertEqual((report.acknowledged, report.new_frames, report.new_receipts), (4, 3, 1))
        self.assertEqual(RunStore(self.db).load(run_id).position, 4)
        stored = self.db.execute('SELECT count(*) FROM source_receipts').fetchone()[0]
        self.assertEqual(stored, 1)
        saved = SourceAnchor.from_data(
            json.loads(self.db.execute('SELECT material FROM source_receipts').fetchone()['material'])
            ['anchor'])
        self.assertEqual(saved.to_data(), anchor)

    def test_omitted_frame_refused_without_advancing(self):
        portal = ReplayPortal(omit=(15,))
        self.addCleanup(portal.shutdown)
        run_id = start(
            self.db, portal, STATION, self.default_id, CONDITION, START, source_end=END,
        )
        handle, = portal.active_handles
        queue_frames(portal, handle)
        portal.emit_source_receipt(handle, START, END)
        with self.assertRaisesRegex(ValueError, 'exactly match'):
            step(self.db, portal, run_id)
        run = RunStore(self.db).load(run_id)
        self.assertEqual(run.position, 2)
        frames = FrameLedger(self.db).in_range(run_id, Interval(START, END))
        self.assertEqual([frame.seq for frame in frames], [11, 14])
        self.assertEqual(self.db.execute('SELECT count(*) FROM source_receipts').fetchone()[0], 0)
        # Sealing without an admitted receipt reports the whole-shift gap.
        outcome = seal(self.db, run_id, END, portal=portal)
        self.assertIsNone(outcome.result_id)
        self.assertEqual(outcome.missing, (Interval(START, END),))

    def test_wrong_value_extra_frame_and_binding_refused(self):
        def value_edit(delivery):
            if isinstance(delivery.event, RawFrame) and delivery.event.seq == 15:
                return replace(delivery, event=replace(delivery.event, value=999))
            return delivery

        def extra_edit(delivery):
            if isinstance(delivery.event, RawFrame) and delivery.event.seq == 15:
                return replace(delivery, event=RawFrame(
                    13, 'salinity', 5, delivery.event.observer, 'injected/13'))
            return delivery

        def binding_edit(delivery):
            if isinstance(delivery.event, RawFrame):
                return replace(delivery, event=replace(delivery.event, observer='other/observer'))
            return delivery

        for label, edit in (('value', value_edit), ('extra', extra_edit), ('binding', binding_edit)):
            with self.subTest(case_id='source_admission.edit', input=label):
                portal = ReplayPortal(edit=edit)
                self.addCleanup(portal.shutdown)
                db = open_database(':memory:')
                self.addCleanup(db.close)
                initialize_defaults(db)
                run_id = start(db, portal, STATION, self.default_id, CONDITION, START, source_end=END)
                handle, = portal.active_handles
                queue_frames(portal, handle)
                portal.emit_source_receipt(handle, START, END)
                with self.assertRaises(ValueError):
                    step(db, portal, run_id)
                # The receipt is the last of four deliveries and never commits.
                self.assertLess(RunStore(db).load(run_id).position, 4)
                self.assertEqual(db.execute('SELECT count(*) FROM source_receipts').fetchone()[0], 0)

    def test_ordinary_v1_receipt_refused_in_source_mode(self):
        run_id, handle = self.open_source_run()
        self.portal.emit_receipt(handle, START, END)
        with self.assertRaisesRegex(ValueError, 'ordinary V1 coverage receipts'):
            step(self.db, self.portal, run_id)
        self.assertEqual(RunStore(self.db).load(run_id).position, 3)

    def test_source_receipt_rejected_in_v1_run(self):
        run_id = start(self.db, self.portal, STATION, self.default_id, CONDITION, START)
        handle, = self.portal.active_handles
        self.portal.advance(*SOURCE_ROWS)
        self.portal.emit_source_receipt(handle, START, END)
        with self.assertRaisesRegex(ValueError, 'source-proof shift binding'):
            step(self.db, self.portal, run_id)

    def test_replay_is_idempotent_and_conflicts_cannot_change_set(self):
        run_id, handle = self.open_source_run()
        self.emit_claim(handle)
        first = step(self.db, self.portal, run_id)
        self.assertEqual(first.new_receipts, 1)
        # Re-emitting the same fixed claim queues the identical receipt again.
        self.portal.emit_source_receipt(handle, START, END)
        replay = step(self.db, self.portal, run_id)
        self.assertEqual((replay.new_receipts, replay.new_frames), (0, 0))
        self.assertEqual(self.db.execute('SELECT count(*) FROM source_receipts').fetchone()[0], 1)
        # A new in-range frame contradicting a certified empty position is refused.
        self.portal.append_event(handle, RawFrame(
            12, 'salinity', 3, handle, f'{STATION}/source/12'))
        with self.assertRaisesRegex(ValueError, 'certified empty position'):
            step(self.db, self.portal, run_id)

    def test_out_of_range_frame_kept_out_of_evidence(self):
        run_id, handle = self.open_source_run()
        self.portal.append_event(handle, RawFrame(
            END, 'salinity', 4, handle, f'{STATION}/source/{END}'))
        report = step(self.db, self.portal, run_id)
        self.assertEqual(report.new_frames, 3)
        self.assertFalse(self.db.execute(
            'SELECT 1 FROM frames WHERE run_id = ? AND seq = ?', (run_id, str(END))).fetchall())

    def test_anchor_is_independent_of_delivery_stream(self):
        run_id, handle = self.open_source_run()
        delivery, anchor = self.emit_claim(handle)
        self.assertIsInstance(delivery.event, SourceReceipt)
        # Editing the queued delivery material cannot move the fixed anchor.
        tampered = replace(delivery.event, material=json.loads(json.dumps(delivery.event.material)))
        tampered.material['anchor']['commitment'] = 'x' * 64
        self.portal.append_event(handle, tampered)
        self.assertEqual(self.portal.source_anchor(handle, START, END), anchor)
        with self.assertRaises(ValueError):
            step(self.db, self.portal, run_id)

    def test_missing_trusted_anchor_refused(self):
        run_id, handle = self.open_source_run()
        delivery, _ = self.emit_claim(handle)
        # A portal without the fixed anchor cannot provide the trusted entry.
        original = type(self.portal).source_anchor

        def missing_anchor(self, observer, start, end):
            raise KeyError('no source anchor')

        type(self.portal).source_anchor = missing_anchor
        try:
            with self.assertRaisesRegex(ValueError, 'no trusted portal anchor'):
                step(self.db, self.portal, run_id)
        finally:
            type(self.portal).source_anchor = original
        self.assertEqual(RunStore(self.db).load(run_id).position, 3)


class TestSourceSealAndArchive(SourceProofCase):
    def _sealed(self):
        run_id, handle = self.open_source_run()
        _, anchor = self.emit_claim(handle)
        step(self.db, self.portal, run_id)
        outcome = seal(self.db, run_id, END, portal=self.portal)
        return run_id, outcome.result_id, anchor

    def test_seal_requires_bound_cutoff(self):
        run_id, handle = self.open_source_run()
        self.emit_claim(handle)
        step(self.db, self.portal, run_id)
        with self.assertRaisesRegex(ValueError, 'bound cutoff'):
            seal(self.db, run_id, END - 1, portal=self.portal)
        self.assertEqual(RunStore(self.db).load(run_id).status, RunStatus.ACTIVE)

    def test_successful_seal_freezes_source_evidence(self):
        run_id, result_id, anchor = self._sealed()
        run = RunStore(self.db).load(run_id)
        self.assertEqual((run.status, run.sealed_end, run.result_id),
                         (RunStatus.SEALED, END, result_id))
        artifact = get(self.db, result_id)
        self.assertEqual([(f.seq, f.value) for f in artifact.frames],
                         [(11, 7), (14, -2), (15, 12)])
        self.assertEqual(artifact.no_match,
                         (Interval(10, 11), Interval(12, 14), Interval(16, 20)))
        self.assertEqual(artifact.source_proof['anchor'], anchor)

    def test_retry_keeps_result_identity(self):
        run_id, result_id, _ = self._sealed()
        again = seal(self.db, run_id, END, portal=self.portal)
        self.assertEqual(again.result_id, result_id)

    def test_commit_failure_preserves_prior_state_and_retries(self):
        run_id, handle = self.open_source_run()
        self.emit_claim(handle)
        step(self.db, self.portal, run_id)
        self.db.executescript('''
            CREATE TABLE allowed_commit(run_id INTEGER PRIMARY KEY);
            CREATE TABLE commit_guard (
                run_id INTEGER REFERENCES allowed_commit(run_id) DEFERRABLE INITIALLY DEFERRED
            );
            CREATE TRIGGER reject_commit AFTER UPDATE OF status ON runs
            WHEN NEW.status = 'sealed'
            BEGIN INSERT INTO commit_guard(run_id) VALUES (NEW.run_id); END;
        ''')
        with self.assertRaises(sqlite3.IntegrityError):
            seal(self.db, run_id, END, portal=self.portal)
        self.assertFalse(self.db.in_transaction)
        self.assertEqual(RunStore(self.db).load(run_id).status, RunStatus.ACTIVE)
        self.assertEqual(self.db.execute('SELECT count(*) FROM results').fetchone()[0], 0)
        self.db.execute('DROP TRIGGER reject_commit')
        retried = seal(self.db, run_id, END, portal=self.portal)
        self.assertIsNotNone(retried.result_id)
        self.assertEqual([f.seq for f in get(self.db, retried.result_id).frames], [11, 14, 15])

    def test_damaged_source_evidence_refused(self):
        _, result_id, _ = self._sealed()
        path = self.path.with_name('damaged-source.sqlite')
        with closing(open_database(path)) as damaged:
            self.db.backup(damaged)
            row = damaged.execute(
                'SELECT payload FROM results WHERE result_id = ?', (result_id,)).fetchone()
            data = json.loads(row['payload'])
            data['source_proof']['material']['leaves'][1]['digest'] = '0' * 64
            modified = json.dumps(data, sort_keys=True, separators=(',', ':'))
            damaged.execute('UPDATE results SET payload = ? WHERE result_id = ?',
                            (modified, result_id))
        with closing(open_database(path)) as damaged:
            with self.assertRaises(StoredEvidenceError):
                get(damaged, result_id)


class TestSourceSegments(SourceProofCase):
    def setUp(self):
        super().setUp()
        run_id, handle = self.open_source_run()
        _, self.anchor = self.emit_claim(handle)
        step(self.db, self.portal, run_id)
        outcome = seal(self.db, run_id, END, portal=self.portal)
        self.result_id = outcome.result_id
        self.run_id = run_id

    def test_export_and_offline_verify(self):
        bundle = export_source_segment(self.db, self.result_id, 13, 16)
        self.assertEqual(bundle['range'], [13, 16])
        self.assertEqual(bundle['anchor'], self.anchor)
        frames, no_match = verify_source_segment(bundle, self.anchor, 13, 16)
        self.assertEqual([(f.seq, f.value) for f in frames], [(14, -2), (15, 12)])
        self.assertEqual(no_match, (Interval(13, 14),))

    def test_empty_sub_interval(self):
        bundle = export_source_segment(self.db, self.result_id, 12, 14)
        frames, no_match = verify_source_segment(bundle, self.anchor, 12, 14)
        self.assertEqual(frames, ())
        self.assertEqual(no_match, (Interval(12, 14),))

    def test_outside_values_and_opening_material_not_exported(self):
        bundle = export_source_segment(self.db, self.result_id, 13, 16)
        hidden = next(leaf for leaf in bundle['leaves'] if leaf['seq'] == 11)
        self.assertEqual(set(hidden), {'seq', 'kind', 'salt', 'digest'})
        self.assertNotIn('frame', hidden)
        serialized = json.dumps(bundle)
        self.assertNotIn('"value":7', serialized)
        # No unsalted fingerprint of the low-cardinality value is present.
        import hashlib
        unsalted = hashlib.sha256(json.dumps(
            {'kind': 'frame', 'frame': {'seq': 11, 'channel': 'salinity', 'value': 7,
                                        'observer': self.anchor['observer'],
                                        'provenance': f'{STATION}/source/11'}},
            sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        self.assertNotIn(unsalted, serialized)

    def test_segment_validation(self):
        for args in ((13, 13), (13, 21), (9, 14), (15, 14)):
            with self.subTest(args=args):
                with self.assertRaises(ValueError):
                    export_source_segment(self.db, self.result_id, *args)
        with self.assertRaises(KeyError):
            export_source_segment(self.db, 'shift-999', 13, 16)

    def test_tamper_refusals(self):
        bundle = export_source_segment(self.db, self.result_id, 13, 16)

        deleted = json.loads(json.dumps(bundle))
        deleted['frames'] = [item for item in deleted['frames'] if item['seq'] != 15]
        with self.assertRaises(ValueError):
            verify_source_segment(deleted, self.anchor, 13, 16)

        rewritten = json.loads(json.dumps(bundle))
        for item in rewritten['frames']:
            if item['seq'] == 14:
                item['value'] = 99
        with self.assertRaises(ValueError):
            verify_source_segment(rewritten, self.anchor, 13, 16)

        swapped = json.loads(json.dumps(self.anchor))
        swapped['observer'] += '-impostor'
        with self.assertRaises(ValueError):
            verify_source_segment(bundle, swapped, 13, 16)

        with self.assertRaises(ValueError):
            verify_source_segment(bundle, self.anchor, 12, 15)

        # A hidden out-of-segment leaf whose digest is edited breaks the root.
        broken = json.loads(json.dumps(bundle))
        for leaf in broken['leaves']:
            if leaf['seq'] == 11:
                leaf['digest'] = 'f' * 64
        with self.assertRaisesRegex(ValueError, 'commitment'):
            verify_source_segment(broken, self.anchor, 13, 16)

    def test_reopen_database_and_verify_without_portal(self):
        self.portal.shutdown()
        db = self.reopen()
        bundle = export_source_segment(db, self.result_id, 13, 16)
        # No portal or db argument is used during verification.
        frames, no_match = verify_source_segment(bundle, self.anchor, 13, 16)
        self.assertEqual([f.seq for f in frames], [14, 15])
        self.assertEqual(no_match, (Interval(13, 14),))

    def test_v1_result_exports_none(self):
        portal = ReplayPortal()
        self.addCleanup(portal.shutdown)
        db = open_database(':memory:')
        self.addCleanup(db.close)
        initialize_defaults(db)
        run_id = start(db, portal, STATION, self.default_id, CONDITION, START)
        handle, = portal.active_handles
        queue_frames(portal, handle)
        portal.emit_receipt(handle, START, END)
        step(db, portal, run_id)
        result_id = seal(db, run_id, END, portal=portal).result_id
        self.assertIsNone(export_source_segment(db, result_id, 13, 16))
