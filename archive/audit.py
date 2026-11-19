from archive.artifacts import artifact_digest, decode_artifact, encode_artifact
from archive.eligibility import validate_evidence_binding
from archive.results import ResultStore, result_id_for
from evidence.intake import CaptureContext
from evidence.intervals import validate_interval
from evidence.source_proofs import SourceAnchor, material_frames, verify_source_material
from quality.evaluation import evaluate_shift
from registry.channels import normalize_channels, require_name
from sessions.runs import validate_position


class StoredEvidenceError(ValueError):
    pass


def _verify_source_proof(artifact):
    source_proof = artifact.source_proof
    if source_proof is None:
        return
    if set(source_proof) != {'anchor', 'material'}:
        raise ValueError('source proof has an unexpected shape')
    stored_anchor = SourceAnchor.from_data(source_proof['anchor'])
    anchor, leaves = verify_source_material(
        source_proof['material'],
        station=artifact.station, observer=artifact.observer, condition=artifact.condition,
        start=artifact.interval.start, end=artifact.interval.end,
    )
    if anchor != stored_anchor:
        raise ValueError('frozen source anchor does not match its receipt material')
    if anchor.range != artifact.interval:
        raise ValueError('source anchor range does not match the frozen interval')
    if artifact.frames != material_frames(leaves):
        raise ValueError('frozen frames do not match the certified source records')
    empty_positions = [
        leaf['seq'] for leaf in leaves
        if leaf['kind'] == 'empty'
        and artifact.interval.start <= leaf['seq'] < artifact.interval.end
    ]
    rebuilt_empty = []
    for span in artifact.no_match:
        rebuilt_empty.extend(range(span.start, span.end))
    if empty_positions != rebuilt_empty:
        raise ValueError('frozen empty positions do not match the certified source records')


def verify_artifact(stored):
    try:
        if type(stored.payload) is not str or artifact_digest(stored.payload) != stored.digest:
            raise ValueError('artifact digest mismatch')
        artifact = decode_artifact(stored.payload)
        if encode_artifact(artifact) != stored.payload:
            raise ValueError('noncanonical artifact')
        validate_position(artifact.run_id, 'run identity')
        validate_position(artifact.first_seq, 'first eligible sequence')
        validate_interval(artifact.interval)
        require_name(artifact.observer, 'observer')
        for interval in artifact.no_match:
            validate_interval(interval, allow_empty=False)
        if (artifact.run_id == 0 or stored.run_id != artifact.run_id
                or artifact.result_id != stored.result_id
                or artifact.result_id != result_id_for(artifact.run_id)
                or stored.end != artifact.interval.end
                or artifact.station != artifact.revision.station
                or artifact.condition != normalize_channels(artifact.condition)):
            raise ValueError('artifact identity or fixed binding mismatch')
        context = CaptureContext(
            artifact.run_id, artifact.interval.start, artifact.condition,
            artifact.observer, artifact.first_seq,
        )
        report = validate_evidence_binding(context, artifact.interval, artifact.frames, artifact.receipts)
        if report.missing or report.no_match != artifact.no_match:
            raise ValueError('frozen coverage does not match original evidence')
        expected = evaluate_shift(artifact.frames, artifact.revision, artifact.condition)
        if expected != artifact.quality:
            raise ValueError('frozen quality does not match the original evidence')
        _verify_source_proof(artifact)
        return artifact
    except (TypeError, AttributeError, ValueError, KeyError, OverflowError, RecursionError) as exc:
        raise StoredEvidenceError(f'damaged evidence for {stored.result_id!r}: {exc}') from exc


def get(db, result_id):
    try:
        stored = ResultStore(db).load(result_id)
    except (TypeError, ValueError) as exc:
        raise StoredEvidenceError(f'damaged result index for {result_id!r}') from exc
    return verify_artifact(stored)
