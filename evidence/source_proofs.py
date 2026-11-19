"""Whole-shift source receipts, portal anchors and offline segment verification.

A source receipt certifies that the frames actually collected for a fixed
observation over the whole shift are exactly the portal's matching source
records.  The portal commitment is a salted Merkle-style digest over every
position of the interval, including empty positions.  Empty positions carry
only salted leaf digests, so a low-cardinality integer value cannot be
enumerated from the exported material.
"""
from dataclasses import dataclass
from hashlib import sha256
import json

from evidence.frames import RawFrame
from evidence.intervals import Interval, validate_interval
from registry.channels import normalize_channels, require_name
from sessions.runs import validate_position


def canonical_json(data):
    return json.dumps(data, sort_keys=True, separators=(',', ':'), allow_nan=False)


def digest_json(data):
    return sha256(canonical_json(data).encode('utf-8')).hexdigest()


def frame_identity(frame):
    # The five RawFrame fields, including the first provenance.
    return {
        'seq': frame.seq,
        'channel': frame.channel,
        'value': frame.value,
        'observer': frame.observer,
        'provenance': frame.provenance,
    }


def frame_leaf(frame, salt):
    return digest_json({'kind': 'frame', 'frame': frame_identity(frame), 'salt': salt})


def empty_leaf(seq, salt):
    return digest_json({'kind': 'empty', 'seq': seq, 'salt': salt})


def compute_commitment(anchor_fields, leaves):
    """Root commitment authenticating the binding and the full match set."""
    return digest_json({
        'station': anchor_fields['station'],
        'observer': anchor_fields['observer'],
        'condition': list(anchor_fields['condition']),
        'range': list(anchor_fields['range']),
        'receipt_id': anchor_fields['receipt_id'],
        'leaves': [leaf['digest'] for leaf in leaves],
    })


@dataclass(frozen=True)
class SourceAnchor:
    station: str
    observer: str
    condition: tuple[str, ...]
    range: Interval
    receipt_id: str
    commitment: str

    def to_data(self):
        return {
            'station': self.station,
            'observer': self.observer,
            'condition': list(self.condition),
            'range': [self.range.start, self.range.end],
            'receipt_id': self.receipt_id,
            'commitment': self.commitment,
        }

    @classmethod
    def from_data(cls, data):
        if type(data) is not dict:
            raise ValueError('source anchor must be a JSON object')
        try:
            condition = normalize_channels(data['condition'])
            lo, hi = data['range']
            anchor = cls(
                require_name(data['station'], 'station'),
                require_name(data['observer'], 'observer'),
                condition,
                validate_interval(Interval(validate_position(lo), validate_position(hi)),
                                  allow_empty=False),
                require_name(data['receipt_id'], 'receipt identity'),
                require_name(data['commitment'], 'commitment'),
            )
        except (TypeError, KeyError) as exc:
            raise ValueError(f'malformed source anchor: {exc}') from exc
        if type(data['commitment']) is not str or not data['commitment']:
            raise ValueError('source anchor requires a non-empty commitment')
        return anchor


def build_source_claim(*, station, observer, condition, start, end, receipt_id, frames, salting):
    """Build receipt material and the independently storable anchor.

    ``frames`` are the portal's actual matching source records in source
    order.  ``salting`` maps each interval position to a non-empty salt.
    """
    condition = normalize_channels(condition)
    interval = validate_interval(Interval(validate_position(start), validate_position(end)),
                                 allow_empty=False)
    if type(receipt_id) is not str or not receipt_id:
        raise ValueError('source receipt requires an identity')
    previous = interval.start - 1
    by_seq = {}
    for frame in frames:
        if not isinstance(frame, RawFrame):
            raise ValueError('source record must be a raw frame')
        if not interval.start <= frame.seq < interval.end or frame.seq <= previous:
            raise ValueError('source frames must be ordered and inside the certified interval')
        if frame.observer != observer or frame.channel not in condition:
            raise ValueError('source frame is outside the observation binding')
        if type(frame.value) is not int:
            raise ValueError('source frame value must be an integer')
        require_name(frame.provenance, 'frame provenance')
        previous = frame.seq
        by_seq[frame.seq] = frame
    leaves = []
    for seq in range(interval.start, interval.end):
        salt = salting.get(seq)
        if type(salt) is not str or not salt:
            raise ValueError(f'missing salt for position {seq}')
        if seq in by_seq:
            leaves.append({
                'seq': seq,
                'kind': 'frame',
                'salt': salt,
                'frame': frame_identity(by_seq[seq]),
                'digest': frame_leaf(by_seq[seq], salt),
            })
        else:
            leaves.append({
                'seq': seq,
                'kind': 'empty',
                'salt': salt,
                'digest': empty_leaf(seq, salt),
            })
    anchor_fields = {
        'station': require_name(station, 'station'),
        'observer': observer,
        'condition': condition,
        'range': (interval.start, interval.end),
        'receipt_id': receipt_id,
    }
    commitment = compute_commitment(anchor_fields, leaves)
    anchor = SourceAnchor(
        station, observer, condition, interval, receipt_id, commitment,
    )
    material = {
        'version': 1,
        'anchor': anchor.to_data(),
        'leaves': leaves,
    }
    return anchor, material


def _require_anchor_binding(anchor, *, station, observer, condition, start, end, receipt_id):
    if (anchor.station != station or anchor.observer != observer
            or anchor.condition != normalize_channels(condition)
            or anchor.range != Interval(start, end) or anchor.receipt_id != receipt_id):
        raise ValueError('source anchor does not match the receipt binding')


def verify_source_material(material, *, station, observer, condition, start, end):
    """Verify receipt material against its embedded anchor and return leaves."""
    if type(material) is not dict or material.get('version') != 1:
        raise ValueError('unsupported source receipt material')
    anchor = SourceAnchor.from_data(material['anchor'])
    _require_anchor_binding(
        anchor, station=station, observer=observer, condition=condition,
        start=start, end=end, receipt_id=anchor.receipt_id,
    )
    leaves = material.get('leaves')
    if type(leaves) is not list or len(leaves) != end - start:
        raise ValueError('source receipt does not cover every interval position')
    expected_seq = start
    rebuilt = []
    for leaf in leaves:
        if type(leaf) is not dict or leaf.get('seq') != expected_seq:
            raise ValueError('source receipt leaf is out of order')
        kind = leaf.get('kind')
        salt = leaf.get('salt')
        if type(salt) is not str or not salt:
            raise ValueError('source receipt leaf is missing its salt')
        if kind == 'frame':
            try:
                frame = RawFrame(**leaf['frame'])
            except (TypeError, KeyError) as exc:
                raise ValueError(f'malformed source frame leaf: {exc}') from exc
            if frame.seq != expected_seq or frame.observer != observer \
                    or frame.channel not in condition or type(frame.value) is not int:
                raise ValueError('source frame leaf breaks the observation binding')
            require_name(frame.provenance, 'frame provenance')
            digest = frame_leaf(frame, salt)
        elif kind == 'empty':
            frame = None
            digest = empty_leaf(expected_seq, salt)
        else:
            raise ValueError('unknown source receipt leaf kind')
        if leaf.get('digest') != digest:
            raise ValueError('source receipt leaf digest does not match its opening material')
        rebuilt.append(digest)
        expected_seq += 1
    anchor_fields = {
        'station': anchor.station, 'observer': anchor.observer,
        'condition': anchor.condition, 'range': (start, end),
        'receipt_id': anchor.receipt_id,
    }
    if compute_commitment(anchor_fields, material['leaves']) != anchor.commitment:
        raise ValueError('portal commitment does not authenticate the source receipt')
    return anchor, leaves


def material_frames(leaves):
    frames = []
    for leaf in leaves:
        if leaf['kind'] == 'frame':
            frames.append(RawFrame(**leaf['frame']))
    return tuple(frames)


def verify_anchor_commitment(anchor, leaves):
    """Re-check a saved trusted anchor against receipt leaves."""
    anchor_fields = {
        'station': anchor.station, 'observer': anchor.observer,
        'condition': anchor.condition,
        'range': (anchor.range.start, anchor.range.end),
        'receipt_id': anchor.receipt_id,
    }
    if compute_commitment(anchor_fields, leaves) != anchor.commitment:
        raise ValueError('trusted anchor does not match the receipt commitment')


def verify_segment_bundle(bundle, trusted_anchor, start, end):
    """Offline verification against a separately saved trusted anchor."""
    trusted = SourceAnchor.from_data(trusted_anchor)
    if type(bundle) is not dict or bundle.get('version') != 1:
        raise ValueError('unsupported source segment bundle')
    lo, hi = bundle.get('range', (None, None))
    if (type(lo) is not int or type(hi) is not int or lo != start or hi != end
            or not lo <= hi or start < trusted.range.start or end > trusted.range.end
            or start == end):
        raise ValueError('authorized segment range is invalid or outside the certified shift')
    anchor = SourceAnchor.from_data(bundle.get('anchor'))
    if anchor != trusted:
        raise ValueError('bundle anchor does not match the independently trusted anchor')
    leaves = bundle.get('leaves')
    if type(leaves) is not list:
        raise ValueError('bundle is missing commitment leaves')
    leaf_by_seq = {}
    for leaf in leaves:
        if type(leaf) is not dict or type(leaf.get('seq')) is not int:
            raise ValueError('malformed commitment leaf')
        seq = leaf['seq']
        if not trusted.range.start <= seq < trusted.range.end or seq in leaf_by_seq:
            raise ValueError('commitment leaf is outside or duplicated within the shift')
        kind = leaf.get('kind')
        salt = leaf.get('salt')
        if type(salt) is not str or not salt:
            raise ValueError('commitment leaf is missing its salt')
        inside = start <= seq < end
        if kind == 'frame':
            if 'frame' in leaf:
                try:
                    frame = RawFrame(**leaf['frame'])
                except (TypeError, KeyError) as exc:
                    raise ValueError(f'malformed frame leaf: {exc}') from exc
                if frame.seq != seq or frame.observer != trusted.observer \
                        or frame.channel not in trusted.condition or type(frame.value) is not int:
                    raise ValueError('frame leaf breaks the source identity')
                require_name(frame.provenance, 'frame provenance')
                if leaf['digest'] != frame_leaf(frame, salt):
                    raise ValueError('frame leaf digest mismatch')
            else:
                # Hidden out-of-segment position: only the salted digest is
                # exported.  It is still authenticated by the root commitment.
                if inside:
                    raise ValueError('authorized segment frame is missing its opening material')
                frame = None
        elif kind == 'empty':
            frame = None
            if leaf['digest'] != empty_leaf(seq, salt):
                raise ValueError('empty leaf digest mismatch')
        else:
            raise ValueError('unknown commitment leaf kind')
        leaf_by_seq[seq] = (kind, frame)
    verify_anchor_commitment(trusted, leaves)

    frames_data = bundle.get('frames')
    if type(frames_data) is not list:
        raise ValueError('bundle is missing its frame list')
    frames = []
    previous = start - 1
    for item in frames_data:
        if type(item) is not dict:
            raise ValueError('bundle frame must be an object')
        try:
            frame = RawFrame(
                item['seq'], item['channel'], item['value'], item['observer'], item['provenance'],
            )
        except (TypeError, KeyError) as exc:
            raise ValueError(f'malformed bundle frame: {exc}') from exc
        if not start <= frame.seq < end or frame.seq <= previous:
            raise ValueError('bundle frames must stay ordered inside the authorized segment')
        if frame.observer != trusted.observer or frame.channel not in trusted.condition:
            raise ValueError('bundle frame is bound to a different observation')
        if type(frame.value) is not int:
            raise ValueError('bundle frame value must be an integer')
        previous = frame.seq
        leaf_entry = leaf_by_seq.get(frame.seq)
        if leaf_entry is None or leaf_entry[0] != 'frame' or leaf_entry[1] != frame:
            raise ValueError('bundle frame is missing from or altered against the commitment')
        frames.append(frame)
    # Every certified frame position inside the segment must be delivered.
    certified = {seq for seq, (kind, _) in leaf_by_seq.items()
                 if kind == 'frame' and start <= seq < end}
    if certified != {frame.seq for frame in frames}:
        raise ValueError('bundle is missing a certified frame or carries an extra frame')

    no_match = []
    cursor = start
    for frame in frames:
        if cursor < frame.seq:
            no_match.append(Interval(cursor, frame.seq))
        cursor = frame.seq + 1
    if cursor < end:
        no_match.append(Interval(cursor, end))
    return tuple(frames), tuple(no_match)
