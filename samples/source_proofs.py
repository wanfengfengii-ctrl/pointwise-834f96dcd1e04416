from contextlib import closing
from dataclasses import replace
import json

from archive import get, seal
from database import open_database
from evidence.frames import RawFrame
from registry import initialize_defaults
from samples.portal_fixture import STATION, ScriptedPortal
from sessions.driver import step
from sessions.opening import start

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
    print(json.dumps({"complete": complete, "omitted_15": omitted}, sort_keys=True))


if __name__ == "__main__":
    main()
