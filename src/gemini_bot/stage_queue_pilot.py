"""Offline candidate builder. Never loads credentials, contacts Gemini or enables trading.

Review values are explicit human/operator inputs, not automatically inferred
calibration. Candidate validation is not evidence sufficiency or live authority.
"""
from __future__ import annotations

import argparse
from dataclasses import replace
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re

from .config import DEFAULT_POLICY_PATH, load_policy
from .queue_pilot import parse_pilot_policy
from .adaptive_queue import parse_adaptive_policy
from .queue_shadow import aware


def read_document(path: Path) -> dict:
    if path.stat().st_size > 1024 * 1024:
        raise ValueError("review/policy document exceeds size bound")
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON field")
            result[key] = value
        return result
    value = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=unique)
    if not isinstance(value, dict):
        raise ValueError("expected JSON object")
    return value


def build_candidate(base_path: Path, review: dict, *, now: datetime) -> dict:
    aware(now)
    base = read_document(base_path)
    strategy, _ = load_policy(base_path)
    if base['policy_version'] != 2 or strategy.queue_pilot is not None:
        raise ValueError("base must be an unchanged disabled-pilot v2 policy")
    if set(review) != {'reviewed_at', 'incumbent_symbols', 'queue_pilot'}:
        raise ValueError("review requires timestamp, incumbent snapshot and explicit calibration")
    reviewed = datetime.fromisoformat(review['reviewed_at'])
    aware(reviewed)
    if reviewed > now:
        raise ValueError("review timestamp is in the future")
    owners = review['incumbent_symbols']
    if (not isinstance(owners, list) or not 0 <= len(owners) <= 20
            or any(not isinstance(s, str) or not s or s.strip().upper() != s for s in owners)
            or len(set(owners)) != len(owners)):
        raise ValueError("invalid incumbent symbol snapshot")
    adaptive = isinstance(review['queue_pilot'], dict) and 'adaptive' in review['queue_pilot']
    pilot = (parse_adaptive_policy(review['queue_pilot']) if adaptive
             else parse_pilot_policy(review['queue_pilot']))
    if pilot is None or not pilot.symbols.issubset(owners):
        raise ValueError("pilot requires exactly two supplied incumbent symbols")
    if pilot.expired(now):
        raise ValueError("calibration is expired")
    replace(strategy, queue_pilot=pilot).validate()
    version = 5 if adaptive and pilot.scope == 'all_selected' else 4 if adaptive else 3
    return {**base, 'policy_version': version, 'queue_pilot': review['queue_pilot']}


def stage_candidate(base_path: Path, review_path: Path, *, draft_name: str,
                    now: datetime) -> Path:
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,79}', draft_name):
        raise ValueError("draft name must be a simple filename stem")
    review = read_document(review_path)
    candidate = build_candidate(base_path, review, now=now)
    # Fixed non-active destination. Exclusive creation cannot overwrite a prior
    # draft, active config, journal or permit; no policy deployment occurs here.
    root = DEFAULT_POLICY_PATH.parent.parent
    directory = root / 'data' / 'queue-pilot-drafts'
    resolved = directory.resolve()
    if not resolved.is_relative_to(root.resolve()):
        raise ValueError("draft directory resolves outside checkout")
    directory.mkdir(parents=True, exist_ok=True)
    output = directory / (draft_name + '.json')
    with output.open('x', encoding='utf-8') as stream:
        stream.write(json.dumps(candidate, indent=2) + '\n')
    return output


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base-policy', type=Path, default=DEFAULT_POLICY_PATH)
    parser.add_argument('--review', type=Path, required=True)
    parser.add_argument('--draft-name', required=True)
    args = parser.parse_args(argv)
    try:
        path = stage_candidate(args.base_policy, args.review, draft_name=args.draft_name,
                               now=datetime.now(timezone.utc))
        print(json.dumps({'candidate_path': str(path), 'configuration_valid': True,
            'policy_sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
            'evidence_sufficiency_verified': False, 'live_activation_authorized': False,
            'active_policy_changed': False, 'fresh_account_and_permit_required': True}))
        return 0
    except (OSError, ValueError, TypeError, ArithmeticError, KeyError, AttributeError) as exc:
        # Do not echo arbitrary input content, account fields or file contents.
        print(json.dumps({'candidate_created': False, 'error_type': type(exc).__name__,
                          'live_activation_authorized': False}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
