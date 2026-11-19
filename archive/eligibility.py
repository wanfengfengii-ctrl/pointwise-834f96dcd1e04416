from dataclasses import dataclass

from evidence.coverage import inspect_coverage, validate_selection
from evidence.frames import FrameLedger, RawFrame
from evidence.intake import CaptureContext
from evidence.intervals import Interval, validate_interval
from evidence.receipts import CoverageReceipt, ReceiptLedger, SourceReceiptLedger
from evidence.source_proofs import material_frames, verify_source_material


@dataclass(frozen=True)
class SealSelection:
    interval: Interval
    frames: tuple[RawFrame, ...]
    receipts: tuple[CoverageReceipt, ...]
    no_match: tuple[Interval, ...]
    source_proof: dict | None = None


@dataclass(frozen=True)
class SealDecision:
    missing: tuple[Interval, ...]
    selection: SealSelection | None


def validate_evidence_binding(context, interval, frames, receipts):
    if interval.start != context.start_seq:
        raise ValueError('frozen interval must start at the run start')
    validate_selection(context, interval, frames, receipts)
    return inspect_coverage(interval, frames, receipts)


def assess_seal(db, run, end):
    interval = validate_interval(Interval(run.start_seq, end))
    frames = FrameLedger(db).in_range(run.run_id, interval)
    receipts = ReceiptLedger(db).intersecting(run.run_id, interval)
    report = validate_evidence_binding(CaptureContext.from_run(run), interval, frames, receipts)
    if report.missing:
        return SealDecision(report.missing, None)
    return SealDecision((), SealSelection(interval, frames, receipts, report.no_match))


def assess_source_seal(db, run):
    """Source-proof shifts only seal at the bound cutoff with a verified receipt."""
    interval = validate_interval(Interval(run.start_seq, run.source_end), allow_empty=False)
    source_receipts = SourceReceiptLedger(db).load(run.run_id)
    if not source_receipts:
        return SealDecision((interval,), None)
    if len(source_receipts) != 1:
        raise ValueError('source-proof shift must carry exactly one whole-shift receipt')
    receipt = source_receipts[0]
    anchor, leaves = verify_source_material(
        receipt.material,
        station=run.station, observer=run.observer, condition=run.condition,
        start=run.start_seq, end=run.source_end,
    )
    frames = FrameLedger(db).in_range(run.run_id, interval)
    if frames != material_frames(leaves):
        raise ValueError('frozen frames do not exactly match the certified source set')
    receipts = (receipt.coverage,)
    report = validate_evidence_binding(CaptureContext.from_run(run), interval, frames, receipts)
    if report.missing:
        return SealDecision(report.missing, None)
    source_proof = {
        'anchor': anchor.to_data(),
        'material': receipt.material,
    }
    return SealDecision(
        (), SealSelection(interval, frames, receipts, report.no_match, source_proof),
    )
