from dataclasses import dataclass
from enum import Enum

from evidence.frames import FrameLedger, RawFrame, validate_frame
from evidence.intervals import Interval
from evidence.receipts import (
    CoverageReceipt, ReceiptLedger, SourceReceipt, SourceReceiptLedger,
    validate_receipt, validate_source_receipt,
)
from evidence.source_proofs import SourceAnchor, material_frames


@dataclass(frozen=True)
class CaptureContext:
    run_id: int
    start_seq: int
    condition: tuple[str, ...]
    observer: str
    first_seq: int
    station: str | None = None
    source_end: int | None = None

    @classmethod
    def from_run(cls, run):
        return cls(run.run_id, run.start_seq, run.condition, run.observer, run.first_seq,
                   run.station, run.source_end)

    def check_frame(self, frame):
        return validate_frame(
            frame, observer=self.observer, condition=self.condition, first_seq=self.first_seq,
        )

    def check_receipt(self, receipt):
        return validate_receipt(
            receipt, observer=self.observer, condition=self.condition, first_seq=self.first_seq,
        )

    def check_source_receipt(self, receipt):
        if self.source_end is None:
            raise ValueError('source receipts require a source-proof shift binding')
        return validate_source_receipt(
            receipt, station=self.station, observer=self.observer, condition=self.condition,
            first_seq=self.first_seq, start_seq=self.start_seq, source_end=self.source_end,
        )


class IntakeOutcome(Enum):
    NEW_FRAME = 'new frame'
    NEW_RECEIPT = 'new receipt'
    REPLAY = 'replay'
    BEFORE_START = 'before start'


def _certified_empty_sequences(db, context):
    empty = set()
    for receipt in SourceReceiptLedger(db).load(context.run_id):
        for leaf in receipt.material['leaves']:
            if leaf['kind'] == 'empty':
                empty.add(leaf['seq'])
    return empty


def accept_event(db, context, event):
    if isinstance(event, RawFrame):
        context.check_frame(event)
        if context.source_end is not None and event.seq >= context.source_end:
            # Frames outside the bound source interval keep format validation
            # but never enter this shift's evidence.
            return IntakeOutcome.BEFORE_START
        if event.seq < context.start_seq:
            return IntakeOutcome.BEFORE_START
        if context.source_end is not None \
                and event.seq in _certified_empty_sequences(db, context):
            raise ValueError(
                f'frame at sequence {event.seq} contradicts the certified empty position')
        if FrameLedger(db).record(context.run_id, event):
            return IntakeOutcome.NEW_FRAME
        return IntakeOutcome.REPLAY
    if isinstance(event, SourceReceipt):
        raise ValueError('source receipts require verification against the trusted portal anchor')
    if isinstance(event, CoverageReceipt):
        if context.source_end is not None:
            raise ValueError('ordinary V1 coverage receipts cannot certify a source-proof shift')
        context.check_receipt(event)
        if event.interval.end <= context.start_seq:
            return IntakeOutcome.BEFORE_START
        if ReceiptLedger(db).record(context.run_id, event):
            return IntakeOutcome.NEW_RECEIPT
        return IntakeOutcome.REPLAY
    raise ValueError('unknown evidence event')


def accept_source_receipt(db, context, receipt, trusted_anchor):
    """Verify a whole-shift source receipt against an independent anchor.

    The anchor comes from the portal's trusted entry, not from the delivery
    stream.  Missing frames, extra frames, altered values, wrong bindings or
    the absence of a matching anchor all raise ValueError so the driver rolls
    the delivery position back.
    """
    _, anchor, leaves = context.check_source_receipt(receipt)
    if SourceAnchor.from_data(trusted_anchor) != anchor:
        raise ValueError('source receipt has no matching trusted portal anchor')
    expected = material_frames(leaves)
    stored = FrameLedger(db).in_range(
        context.run_id, Interval(context.start_seq, context.source_end),
    )
    if stored != expected:
        raise ValueError('received original frames do not exactly match the portal source set')
    if SourceReceiptLedger(db).record(context.run_id, receipt):
        return IntakeOutcome.NEW_RECEIPT
    return IntakeOutcome.REPLAY
