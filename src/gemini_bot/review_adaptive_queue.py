"""Read-only archive coverage/sensitivity review. Never authorizes configuration.

Minute snapshots cannot replay within-minute queues, cancellation races or exact
fills. This is an intentionally separate stress screen, NOT execution backtesting.
"""
import argparse
from datetime import datetime
from decimal import Decimal as D
import json
from pathlib import Path

from .queue_shadow import aware, nonnegative


def archive_rows(directory):
    seen = set()
    # Same bounded files as ShadowArchive. Oldest first; never read credentials,
    # journal, arbitrary log fields or any other directory entries.
    for name in ('queue-shadow.jsonl.3', 'queue-shadow.jsonl.2', 'queue-shadow.jsonl.1', 'queue-shadow.jsonl'):
        path = directory / name
        if not path.exists():
            continue
        total = 0
        with path.open('rb') as stream:
            while line := stream.readline(512*1024+1):
                total += len(line)
                if len(line) > 512*1024 or total > 64*1024*1024:
                    raise ValueError('archive size bound exceeded')
                if not line.endswith(b'\n'):
                    # An in-progress writer's partial tail is not an observation.
                    break
                row = json.loads(line)
                stamp = datetime.fromisoformat(row['observed_at'])
                aware(stamp)
                if stamp in seen:
                    continue
                seen.add(stamp)
                if len(seen) > 10000:
                    raise ValueError('archive observation bound exceeded')
                yield row


def review(rows, *, buffer, burst_multiplier, depth_multiplier, cancel_budget):
    for value in (buffer, burst_multiplier, depth_multiplier, cancel_budget):
        nonnegative(value)
    if buffer < 15 or burst_multiplier < 1 or depth_multiplier <= 1 or not 0 < cancel_budget <= 60:
        raise ValueError('invalid sensitivity envelope')
    markets = {}
    count, first, last = 0, None, None
    maximum, failures, maximum_first_at = D(0), 0, None
    for row in rows:
        stamp = datetime.fromisoformat(row['observed_at']); aware(stamp)
        if last is not None and stamp <= last:
            raise ValueError('archive chronology is not strictly increasing')
        first, last = first or stamp, stamp
        count += 1
        q = row['queue']
        if q.get('error_type') or q.get('archive_error_type'):
            raise ValueError('observer/archive error requires separate review')
        timing = q['cancel_measurement']
        duration = D(timing['maximum_seconds'] or '0'); nonnegative(duration)
        if duration > maximum:
            maximum, maximum_first_at = duration, stamp.isoformat()
        failures = max(failures, int(timing['failed_or_interrupted_calls']))
        for m in q['markets']:
            s = m['symbol']
            if not isinstance(s, str) or not s or len(s) > 150:
                raise ValueError('invalid archive symbol')
            if s not in markets:
                if len(markets) >= 64:
                    raise ValueError('archive symbol bound exceeded')
                markets[s] = {'complete': 0, 'unavailable': 0, 'positive': [0, 0],
                    'flow': [D(0), D(0)], 'drop': [D(0), D(0)], 'prior': [None, None], 'depths': []}
            data = markets[s]
            if (m.get('availability') != 'scenario' or m.get('full_flow_window_observed') is not True
                    or m.get('trade_time_uncertain') or m.get('flow_history_truncated')):
                data['unavailable'] += 1
                data['prior'] = [None, None]
                continue
            data['complete'] += 1
            depths = []
            for i, side in enumerate(('YES', 'NO')):
                flow = D(m[side.lower()+'_sell_quantity_60s'])
                o = m['outcomes'][side]
                depth, depleted = D(o['external_depth']), D(o['depth_depletion'])
                for value in (flow, depth, depleted): nonnegative(value)
                depths.append(depth)
                data['positive'][i] += int(flow > 0)
                data['flow'][i] = max(data['flow'][i], flow)
                previous = data['prior'][i]
                if previous and previous[0] == o['joined_at'] and depleted >= previous[1]:
                    # This is a whole archive interval's depletion, deliberately
                    # NOT called the runtime's largest single-update drop.
                    data['drop'][i] = max(data['drop'][i], depleted-previous[1])
                data['prior'][i] = (o['joined_at'], depleted)
            if len(data['depths']) >= 10000:
                raise ValueError('market observation bound exceeded')
            data['depths'].append(depths)
    result = []
    for symbol, data in markets.items():
        required = [(buffer + burst_multiplier*(f+d))*depth_multiplier
                    for f, d in zip(data['flow'], data['drop'])]
        passes = sum(all(depth >= threshold for depth, threshold in zip(depths, required))
                     for depths in data['depths'])
        result.append({'symbol': symbol, 'complete_rows': data['complete'],
            'unavailable_rows': data['unavailable'], 'nonzero_side_windows': dict(zip(('YES', 'NO'), data['positive'])),
            'peak_sell_flow_60s': dict(zip(('YES', 'NO'), map(str, data['flow']))),
            'peak_archived_interval_depletion': dict(zip(('YES', 'NO'), map(str, data['drop']))),
            'stress_required_depth': dict(zip(('YES', 'NO'), map(str, required))),
            'depth_pass_rows_under_full_sample_stress': passes})
    return {'mode': 'offline_sensitivity_not_execution_replay', 'live_activation_authorized': False,
        'evidence_sufficiency_verified': False, 'policy_created': False,
        'observations': count, 'first_at': first.isoformat() if first else None,
        'last_at': last.isoformat() if last else None,
        'cancel_maximum_seconds': str(maximum), 'cancel_maximum_first_archive_at': maximum_first_at,
        'failed_or_interrupted_calls_max': failures,
        'cancel_envelope_pass': bool(count and not failures and 0 < maximum <= cancel_budget),
        'scenario_parameters': {k: str(v) for k, v in dict(buffer=buffer, burst_multiplier=burst_multiplier,
            depth_multiplier=depth_multiplier, cancel_budget=cancel_budget).items()},
        'limitations': ['overlapping windows are not independent trades',
            'within-minute paths, queue identity and actual fills cannot be reconstructed',
            'full-sample peaks include future information: sensitivity only, not out-of-sample validation',
            'quiet windows and observed cancellation maxima are not future safety guarantees'],
        'markets': sorted(result, key=lambda r: r['symbol'])}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runtime', type=Path, required=True)
    parser.add_argument('--since', type=datetime.fromisoformat,
                        help='Aware timestamp selecting this measurement run only')
    for name in ('buffer', 'burst-multiplier', 'depth-multiplier', 'cancel-budget'):
        parser.add_argument('--'+name, type=D, required=True)
    args = parser.parse_args(argv)
    try:
        if args.since is not None:
            aware(args.since)
        rows = (r for r in archive_rows(args.runtime)
                if args.since is None or datetime.fromisoformat(r['observed_at']) >= args.since)
        result = review(rows, buffer=args.buffer,
            burst_multiplier=args.burst_multiplier, depth_multiplier=args.depth_multiplier,
            cancel_budget=args.cancel_budget)
        print(json.dumps(result, indent=2))
        return 0
    except (OSError, ValueError, TypeError, ArithmeticError, KeyError, AttributeError) as exc:
        print(json.dumps({'error_type': type(exc).__name__, 'live_activation_authorized': False}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
