from dataclasses import dataclass
import json

from database import read_integer
from evidence.intervals import Interval, intersect_interval, validate_interval
from evidence.source_proofs import canonical_json, verify_source_material
from registry.channels import require_name


@dataclass(frozen=True)
class CoverageReceipt:
    receipt_id: str
    interval: Interval
    condition: tuple[str, ...]
    observer: str
    provenance: str


@dataclass(frozen=True)
class SourceReceipt:
    coverage: CoverageReceipt
    material: dict

    def __post_init__(self):
        if not isinstance(self.coverage, CoverageReceipt):
            raise ValueError('source receipt must wrap a coverage receipt')
        # The material must survive a JSON round trip.
        restored = json.loads(canonical_json(self.material))
        if restored != self.material:
            raise ValueError('source receipt material must be JSON round-trippable')


def validate_receipt(receipt, *, observer, condition, first_seq):
    if not isinstance(receipt, CoverageReceipt):
        raise ValueError('expected a coverage receipt')
    require_name(receipt.receipt_id, 'receipt identity')
    require_name(receipt.provenance, 'receipt provenance')
    validate_interval(receipt.interval, allow_empty=False)
    if receipt.observer != observer or receipt.condition != condition:
        raise ValueError('receipt is bound to a different observation')
    if receipt.interval.start < first_seq:
        raise ValueError('receipt predates observation eligibility')
    return receipt


def _from_row(row):
    condition = json.loads(row['condition'])
    if type(condition) is not list:
        raise ValueError('invalid stored receipt condition')
    return CoverageReceipt(
        row['receipt_id'], Interval(read_integer(row['lo']), read_integer(row['hi'])),
        tuple(condition), row['observer'], row['provenance'],
    )


def receipt_order(receipt):
    return receipt.interval, receipt.observer, receipt.receipt_id


class ReceiptLedger:
    def __init__(self, db):
        self.db = db

    def record(self, run_id, receipt):
        previous = self.db.execute(
            'SELECT * FROM receipts WHERE run_id = ? AND observer = ? AND receipt_id = ?',
            (run_id, receipt.observer, receipt.receipt_id),
        ).fetchone()
        if previous is not None:
            if _from_row(previous) != receipt:
                raise ValueError(f'conflicting receipt {receipt.receipt_id!r}')
            return False
        self.db.execute(
            'INSERT INTO receipts(run_id, observer, receipt_id, lo, hi, condition, provenance) '
            'VALUES (?, ?, ?, ?, ?, ?, ?)',
            (run_id, receipt.observer, receipt.receipt_id, str(receipt.interval.start),
             str(receipt.interval.end), json.dumps(receipt.condition), receipt.provenance),
        )
        return True

    def intersecting(self, run_id, interval):
        rows = self.db.execute('SELECT * FROM receipts WHERE run_id = ?', (run_id,))
        receipts = []
        for row in rows:
            receipt = _from_row(row)
            validate_interval(receipt.interval, allow_empty=False)
            if intersect_interval(receipt.interval, interval) is not None:
                receipts.append(receipt)
        return tuple(sorted(receipts, key=receipt_order))


def validate_source_receipt(receipt, *, station, observer, condition, first_seq, start_seq, source_end):
    if not isinstance(receipt, SourceReceipt):
        raise ValueError('expected a source receipt')
    coverage = validate_receipt(
        receipt.coverage, observer=observer, condition=condition, first_seq=first_seq,
    )
    if coverage.interval != Interval(start_seq, source_end):
        raise ValueError('source receipt must cover the whole bound shift interval')
    anchor, leaves = verify_source_material(
        receipt.material,
        station=station, observer=observer, condition=condition,
        start=start_seq, end=source_end,
    )
    return receipt, anchor, leaves


class SourceReceiptLedger:
    def __init__(self, db):
        self.db = db

    def record(self, run_id, receipt):
        previous = self.db.execute(
            'SELECT material FROM source_receipts WHERE run_id = ? AND receipt_id = ?',
            (run_id, receipt.coverage.receipt_id),
        ).fetchone()
        if previous is not None:
            if previous['material'] != canonical_json(receipt.material):
                raise ValueError(
                    f'conflicting source receipt {receipt.coverage.receipt_id!r}')
            return False
        self.db.execute(
            'INSERT INTO source_receipts(run_id, observer, receipt_id, lo, hi, condition, material) '
            'VALUES (?, ?, ?, ?, ?, ?, ?)',
            (run_id, receipt.coverage.observer, receipt.coverage.receipt_id,
             str(receipt.coverage.interval.start), str(receipt.coverage.interval.end),
             json.dumps(receipt.coverage.condition), canonical_json(receipt.material)),
        )
        return True

    def load(self, run_id):
        rows = self.db.execute(
            'SELECT * FROM source_receipts WHERE run_id = ? ORDER BY receipt_id', (run_id,),
        )
        records = []
        for row in rows:
            condition = tuple(json.loads(row['condition']))
            coverage = CoverageReceipt(
                row['receipt_id'], Interval(read_integer(row['lo']), read_integer(row['hi'])),
                condition, row['observer'], f'{row["observer"]}/source-receipt/{row["receipt_id"]}',
            )
            records.append(SourceReceipt(coverage, json.loads(row['material'])))
        return tuple(records)
