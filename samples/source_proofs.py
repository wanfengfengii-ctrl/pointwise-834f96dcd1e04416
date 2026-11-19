from contextlib import closing
from dataclasses import replace
import json
from pathlib import Path
from tempfile import TemporaryDirectory

from archive import export_source_segment, get, seal, verify_source_segment
from database import open_database
from evidence.frames import FrameLedger, RawFrame
from evidence.intervals import Interval
from registry import initialize_defaults
from samples.portal_fixture import STATION, ScriptedPortal
from sessions.driver import step
from sessions.opening import start
from sessions.runs import RunStore

START = 10
END = 20
CONDITION = ("salinity",)
SOURCE_ROWS = (
    ("turbidity", 20), ("salinity", 7), ("turbidity", 21),
    ("turbidity", 22), ("salinity", -2), ("salinity", 12),
    ("turbidity", 23), ("turbidity", 24), ("turbidity", 25), ("turbidity", 26),
)


class ReplayPortal(ScriptedPortal):
    """Apply a transport fault after the portal has recorded the source and queued events."""

    def __init__(self, *, omit=(), edit=None):
        super().__init__(first_seq=START)
        self.omit = frozenset(omit)
        self.edit = edit

    def poll(self, observer, after):
        delivered = []
        for delivery in super().poll(observer, 0):
            if isinstance(delivery.event, RawFrame) and delivery.event.seq in self.omit:
                continue
            delivered.append(delivery)
        if after > len(delivered):
            raise ValueError("unknown replay position")
        visible = []
        for position, delivery in enumerate(delivered[after:], after + 1):
            if self.edit is not None:
                delivery = self.edit(delivery)
            visible.append(replace(delivery, position=position))
        return tuple(visible)


def queue_frames(portal, observer):
    portal.advance(*SOURCE_ROWS)
    for seq in (15, 14, 11):
        portal.emit_frame(observer, seq)


def legacy_control(*, omit=()):
    portal = ReplayPortal(omit=omit)
    with closing(open_database(":memory:")) as db:
        revision_id = initialize_defaults(db)
        run_id = start(db, portal, STATION, revision_id, CONDITION, START)
        observer, = portal.active_handles
        queue_frames(portal, observer)
        portal.emit_receipt(observer, START, END)
        report = step(db, portal, run_id)
        fixed = seal(db, run_id, END, portal=portal)
        portal.shutdown()
        artifact = get(db, fixed.result_id)
        return {
            "frames": [frame.seq for frame in artifact.frames],
            "no_match": [[span.start, span.end] for span in artifact.no_match],
            "receipt_position": report.position,
            "result_id": artifact.result_id,
        }


def _open_source_run(db, portal):
    revision_id = initialize_defaults(db)
    run_id = start(db, portal, STATION, revision_id, CONDITION, START, source_end=END)
    observer, = portal.active_handles
    queue_frames(portal, observer)
    portal.emit_source_receipt(observer, START, END)
    # The anchor is saved independently from the trusted portal entry.
    anchor = portal.source_anchor(observer, START, END)
    return run_id, observer, anchor


def demo_omission_refusal():
    """漏送拒绝：缺帧凭证不推进交付位置，原帧和已提交位置保留。"""
    portal = ReplayPortal(omit=(15,))
    with closing(open_database(":memory:")) as db:
        run_id, _, _ = _open_source_run(db, portal)
        try:
            step(db, portal, run_id)
            raise AssertionError("omitted frame receipt must be refused")
        except ValueError as exc:
            refusal = str(exc)
        run = RunStore(db).load(run_id)
        frames = FrameLedger(db).in_range(run_id, Interval(START, END))
        seal_outcome = seal(db, run_id, END, portal=portal)
        return {
            "refusal": refusal,
            "position_preserved": run.position,
            "frames_preserved": [frame.seq for frame in frames],
            "seal_without_receipt": [[span.start, span.end] for span in seal_outcome.missing],
            "result_id": seal_outcome.result_id,
        }


def demo_complete_seal(path):
    """完整封存：凭证、锚点和成果同次提交，失败保全先前状态。"""
    portal = ReplayPortal()
    with closing(open_database(path)) as db:
        run_id, observer, anchor = _open_source_run(db, portal)
        report = step(db, portal, run_id)
        outcome = seal(db, run_id, END, portal=portal)
        # Retrying at the same cutoff keeps the original result identity.
        retry = seal(db, run_id, END, portal=portal)
        assert retry.result_id == outcome.result_id
        artifact = get(db, outcome.result_id)
        return {
            "result_id": outcome.result_id,
            "retry_same_result": retry.result_id == outcome.result_id,
            "receipt_position": report.position,
            "frames": [[frame.seq, frame.value] for frame in artifact.frames],
            "no_match": [[span.start, span.end] for span in artifact.no_match],
            "anchor": anchor,
        }


def demo_reopen_claim(path, anchor):
    """停源重开领包：门户停用、库重开后按成果号领取授权子区间。"""
    bundles = {}
    with closing(open_database(path)) as db:
        result_id = f"shift-1"
        get(db, result_id)  # whole-shift pickup still verifies the frozen source evidence
        for start_, end_ in ((13, 16), (12, 14)):
            bundles[f"{start_}-{end_}"] = export_source_segment(db, result_id, start_, end_)
        v1_like = _seal_legacy_for_none_check(db)
        none_export = export_source_segment(db, v1_like, 13, 16)
    return bundles, none_export


def _seal_legacy_for_none_check(db):
    portal = ReplayPortal()
    revision_id = initialize_defaults(db)
    run_id = start(db, portal, STATION, revision_id, CONDITION, START)
    observer, = portal.active_handles
    queue_frames(portal, observer)
    portal.emit_receipt(observer, START, END)
    step(db, portal, run_id)
    outcome = seal(db, run_id, END, portal=portal)
    portal.shutdown()
    return outcome.result_id


def demo_offline_verification(bundles, anchor):
    """断库复核：无门户、无数据库参数，仅凭可信锚点和预期区间核验。"""
    results = {}
    for label, (start_, end_) in (("13-16", (13, 16)), ("12-14", (12, 14))):
        frames, no_match = verify_source_segment(bundles[label], anchor, start_, end_)
        results[label] = {
            "frames": [[frame.seq, frame.value] for frame in frames],
            "no_match": [[span.start, span.end] for span in no_match],
        }
    bundle = bundles["13-16"]
    tamper = {}
    stripped = _json_clone(bundle)
    stripped["frames"] = [item for item in stripped["frames"] if item["seq"] != 15]
    tamper["delete_frame_15"] = _expect_refusal(stripped, anchor, 13, 16)
    rewritten = _json_clone(bundle)
    for item in rewritten["frames"]:
        if item["seq"] == 14:
            item["value"] = 99
    tamper["rewrite_frame_14"] = _expect_refusal(rewritten, anchor, 13, 16)
    swapped = _json_clone(anchor)
    swapped["observer"] = swapped["observer"] + "-impostor"
    tamper["swap_observer"] = _expect_refusal(bundle, swapped, 13, 16)
    tamper["range_mismatch"] = _expect_refusal(bundle, anchor, 12, 15)
    privacy_leaf = next(leaf for leaf in bundle["leaves"] if leaf["seq"] == 11)
    privacy = {
        "seq_11_leaks_frame_material": "frame" in privacy_leaf,
        "seq_11_carries_salted_digest": set(privacy_leaf) == {"seq", "kind", "salt", "digest"},
    }
    return results, tamper, privacy


def _json_clone(value):
    return json.loads(json.dumps(value))


def _expect_refusal(bundle, anchor, start_, end_):
    try:
        verify_source_segment(bundle, anchor, start_, end_)
    except ValueError as exc:
        return str(exc)
    raise AssertionError("tampered bundle must be refused")


def main():
    complete = legacy_control()
    omitted = legacy_control(omit=(15,))
    assert complete == {
        "frames": [11, 14, 15], "no_match": [[10, 11], [12, 14], [16, 20]],
        "receipt_position": 4, "result_id": "shift-1",
    }
    assert omitted == {
        "frames": [11, 14], "no_match": [[10, 11], [12, 14], [15, 20]],
        "receipt_position": 3, "result_id": "shift-1",
    }
    refusal_report = demo_omission_refusal()
    assert refusal_report["position_preserved"] == 2
    assert refusal_report["frames_preserved"] == [11, 14]
    assert refusal_report["result_id"] is None
    with TemporaryDirectory() as directory:
        path = Path(directory) / "source_shift.sqlite"
        sealed = demo_complete_seal(path)
        assert sealed["frames"] == [[11, 7], [14, -2], [15, 12]]
        assert sealed["no_match"] == [[10, 11], [12, 14], [16, 20]]
        anchor = sealed["anchor"]
        bundles, none_export = demo_reopen_claim(path, anchor)
        assert none_export is None
        offline, tamper, privacy = demo_offline_verification(bundles, anchor)
        assert offline["13-16"] == {"frames": [[14, -2], [15, 12]], "no_match": [[13, 14]]}
        assert offline["12-14"] == {"frames": [], "no_match": [[12, 14]]}
        assert privacy == {"seq_11_leaks_frame_material": False,
                           "seq_11_carries_salted_digest": True}
        assert all(isinstance(message, str) and message for message in tamper.values())
    print(json.dumps({
        "v1_complete": complete,
        "v1_omitted_15": omitted,
        "source_omission_refusal": refusal_report,
        "source_complete_seal": sealed,
        "source_offline_verification": offline,
        "source_tamper_refusals": tamper,
        "source_privacy": privacy,
        "v1_export_returns_none": none_export is None,
    }, ensure_ascii=False, sort_keys=True, indent=2))


if __name__ == "__main__":
    main()
