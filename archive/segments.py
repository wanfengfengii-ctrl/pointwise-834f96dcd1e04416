"""Offline authorized segment export and verification for source-proof results."""
import json

from archive.audit import StoredEvidenceError, get
from evidence.intervals import Interval, validate_interval
from evidence.source_proofs import (
    SourceAnchor, canonical_json, frame_identity, verify_segment_bundle,
)


def export_source_segment(db, result_id, start, end):
    """Return a JSON-round-trippable bundle for an authorized sub-interval."""
    artifact = get(db, result_id)
    if artifact.source_proof is None:
        # Readable V1 results have no source commitment and never export here.
        return None
    shift = artifact.interval
    segment = validate_interval(Interval(start, end), allow_empty=False)
    if segment.start < shift.start or segment.end > shift.end:
        raise ValueError('segment must be a non-empty sub-interval of the sealed shift')
    material = artifact.source_proof['material']
    anchor = SourceAnchor.from_data(artifact.source_proof['anchor'])
    leaves = []
    for leaf in material['leaves']:
        seq = leaf['seq']
        if segment.start <= seq < segment.end:
            # Inside the authorized segment: full opening material and frame.
            leaves.append(leaf)
        else:
            # Outside the segment: deliverable position with the hidden salted
            # commitment only.  Original values, opening material and unsalted
            # fingerprints never leave the archive.
            leaves.append({
                'seq': seq,
                'kind': leaf['kind'],
                'salt': leaf['salt'],
                'digest': leaf['digest'],
            })
    frames = [
        frame_identity(frame) for frame in artifact.frames
        if segment.start <= frame.seq < segment.end
    ]
    bundle = {
        'version': 1,
        'range': [segment.start, segment.end],
        'anchor': anchor.to_data(),
        'leaves': leaves,
        'frames': frames,
    }
    restored = json.loads(canonical_json(bundle))
    if restored != bundle:
        raise StoredEvidenceError(f'source segment for {result_id!r} is not JSON serializable')
    return bundle


def verify_source_segment(bundle, trusted_anchor, start, end):
    """Verify a bundle offline using a separately saved trusted anchor."""
    try:
        return verify_segment_bundle(bundle, trusted_anchor, start, end)
    except (TypeError, AttributeError, KeyError, OverflowError, RecursionError) as exc:
        raise ValueError(f'source segment verification failed: {exc}') from exc
