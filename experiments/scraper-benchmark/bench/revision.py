"""Tiny SEAL-added experiment, not a production identity/revision implementation.

Content hash includes title/date/body. Raw hash is over response bytes. No regex
removes dates, money, words such as 'source', or arbitrary business content.
"""
import json
from .common import normalize_text, sha256_hex, sha256_text


VERSION = 'seal-benchmark-canonical-v1'


def content_hash(doc):
    canonical = {name: normalize_text(doc.get(name) or '')
                 for name in ('title', 'published_at', 'body_text')}
    return sha256_text(json.dumps(canonical, ensure_ascii=False, sort_keys=True))


def observation(raw, doc, identity, ordinal):
    raw_hash = sha256_hex(raw)
    return {
        'identity': identity,
        'raw_hash': raw_hash,
        'content_hash': content_hash(doc),
        'canonicalizer_version': VERSION,
        'RawSnapshot': {'id': raw_hash, 'byte_length': len(raw)},
        'FetchObservation': {'ordinal': ordinal, 'status': 200, 'snapshot_id': raw_hash,
                             'source_url': doc['source_url']},
        'Result': doc,
        'FieldEvidence': doc.get('_evidence', {}),
    }
