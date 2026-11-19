from dataclasses import dataclass

from database import transaction
from evidence.intake import (
    CaptureContext, IntakeOutcome, accept_event, accept_source_receipt,
)
from evidence.receipts import SourceReceipt
from sessions.portal import check_delivery
from sessions.runs import RunStatus, RunStore


@dataclass(frozen=True)
class StepReport:
    acknowledged: int
    new_frames: int
    new_receipts: int
    position: int
    status: RunStatus


def step(db, portal, run_id):
    runs = RunStore(db)
    run = runs.load(run_id)
    if run.status is RunStatus.SEALED:
        return StepReport(0, 0, 0, run.position, run.status)
    acknowledged = new_frames = new_receipts = 0
    position = run.position
    for delivery in portal.poll(run.observer, position):
        with transaction(db, write=True):
            current = runs.load(run_id)
            if current.status is not RunStatus.ACTIVE or current.position != position:
                raise RuntimeError('run changed during portal poll')
            check_delivery(delivery, current.observer, current.condition, position)
            context = CaptureContext.from_run(current)
            event = delivery.event
            if isinstance(event, SourceReceipt):
                try:
                    trusted_anchor = portal.source_anchor(
                        current.observer, event.coverage.interval.start,
                        event.coverage.interval.end,
                    )
                except (KeyError, OSError) as exc:
                    raise ValueError('no trusted portal anchor for the source receipt') from exc
                outcome = accept_source_receipt(db, context, event, trusted_anchor)
            else:
                outcome = accept_event(db, context, event)
            runs.advance(run_id, position, delivery.position)
        position = delivery.position
        acknowledged += 1
        new_frames += outcome is IntakeOutcome.NEW_FRAME
        new_receipts += outcome is IntakeOutcome.NEW_RECEIPT
    return StepReport(acknowledged, new_frames, new_receipts, position, run.status)
