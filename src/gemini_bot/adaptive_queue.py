"""Bounded adaptive queue scenarios, NOT measured queue positions or fill protection.

Shared immutable limits need review; synthetic test constants are not defaults.
The complete observed 60-second selling burst is reserved, never divided by 60
to assume smooth arrivals. Peaks cannot decay within a healthy generation.
This module cannot cancel, reconcile, select owners, create authority or send.
"""
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime
from decimal import Decimal as D
import hashlib
import json

from .domain import Outcome, Side
from .guarded_bid import QueueCalibration
from .queue_pilot import QueuePilot
from .queue_shadow import aware, nonnegative
from .capacity import MAX_ACTIVE_MARKETS


@dataclass(frozen=True)
class AdaptiveLimits:
    warmup_seconds: D
    promotion_seconds: D
    observation_gap_seconds: D
    cancel_budget_seconds: D
    buffer_contracts: D
    burst_multiplier: D
    depth_multiplier: D
    minimum_requeue_gain: D
    refresh_cooldown_seconds: D
    expires_at: datetime
    evidence_reference: str

    def __post_init__(self):
        aware(self.expires_at)
        for f in fields(self):
            if f.name in {'expires_at', 'evidence_reference'}:
                continue
            v = getattr(self, f.name)
            nonnegative(v)
            if v <= 0:
                raise ValueError('adaptive limits must be positive')
        if (not 60 <= self.warmup_seconds <= 3600
                or not 1 <= self.promotion_seconds <= 3600
                or not 0 < self.observation_gap_seconds <= 3
                or not 0 < self.cancel_budget_seconds <= 60
                or self.buffer_contracts < 15 or self.burst_multiplier < 1
                or self.depth_multiplier <= 1
                or not isinstance(self.evidence_reference, str)
                or not self.evidence_reference.strip()):
            raise ValueError('invalid adaptive envelope')


@dataclass(frozen=True)
class AdaptivePolicy:
    # Research uses two fixed names; all_selected binds to reconciled ownership.
    market_symbols: tuple[str, ...]
    limits: AdaptiveLimits
    scope: str = 'research'
    placement_mode: str = 'adaptive'

    def __post_init__(self):
        if (not isinstance(self.market_symbols, tuple)
                or self.placement_mode not in {'adaptive', 'stay_best'}
                or (self.placement_mode == 'stay_best' and self.scope != 'all_selected')
                or self.scope not in {'research', 'all_selected'}
                or (self.scope == 'research' and (len(self.market_symbols) != 2
                    or len(set(self.market_symbols)) != 2))
                or (self.scope == 'all_selected' and self.market_symbols != ())
                or any(not isinstance(s, str) or not s or s.strip().upper() != s
                       for s in self.market_symbols)
                or not isinstance(self.limits, AdaptiveLimits)):
            raise ValueError('adaptive scope must be two research symbols or all_selected without names')

    @property
    def symbols(self):
        return frozenset(self.market_symbols)

    def expired(self, now):
        # In stay_best, only optional queue optimization expires.
        return self.placement_mode != 'stay_best' and now >= self.limits.expires_at

    @property
    def breach_key(self):
        # Canonical decimal representation makes equivalent spellings identical.
        limits = {k: (v.isoformat() if isinstance(v, datetime) else
                      str(v.normalize()) if isinstance(v, D) else v)
                  for k, v in asdict(self.limits).items()}
        raw = json.dumps({'symbols': sorted(self.symbols), 'limits': limits}, sort_keys=True)
        if self.scope == 'all_selected':
            # Membership changes must never clear a shared cancellation breach.
            raw = json.dumps({'scope': self.scope, 'limits': limits}, sort_keys=True)
        return 'adaptive_cancel_breach_v1:' + hashlib.sha256(raw.encode()).hexdigest()


def parse_adaptive_policy(raw):
    if (not isinstance(raw, dict) or set(raw) not in ({'symbols', 'adaptive'}, {'scope', 'adaptive'},
                                                   {'scope', 'adaptive', 'placement_mode'})
            or ('placement_mode' in raw and raw['placement_mode'] != 'stay_best')
            or ('symbols' in raw and not isinstance(raw['symbols'], list))
            or ('scope' in raw and raw['scope'] != 'all_selected')
            or not isinstance(raw['adaptive'], dict)
            or set(raw['adaptive']) != {f.name for f in fields(AdaptiveLimits)}):
        raise ValueError('invalid adaptive policy schema')
    parsed = {}
    for key, value in raw['adaptive'].items():
        if key == 'expires_at':
            parsed[key] = datetime.fromisoformat(value)
        elif key == 'evidence_reference':
            parsed[key] = value
        else:
            if isinstance(value, bool) or not isinstance(value, (str, int)):
                raise ValueError('adaptive decimal must be exact text or integer')
            parsed[key] = D(value)
    return AdaptivePolicy(tuple(raw.get('symbols', ())), AdaptiveLimits(**parsed),
                          raw.get('scope', 'research'), raw.get('placement_mode', 'adaptive'))


@dataclass
class Evidence:
    generation: object
    since: datetime
    last_check: datetime | None = None
    good_since: datetime | None = None
    peak_flow: dict = field(default_factory=lambda: {o: D(0) for o in Outcome})
    peak_drop: dict = field(default_factory=lambda: {o: D(0) for o in Outcome})


class AdaptiveQueuePilot(QueuePilot):
    def __init__(self, policy: AdaptivePolicy):
        self._symbols = policy.symbols
        super().__init__(policy)
        self.evidence = {}
        self.modes = {s: self.initial_mode for s in policy.symbols}
        # Initial one-tick placement uses normal execution proofs. Transitions
        # and any subsequent cancellation require the stronger pilot barrier.
        self.proof_required.clear()
        self.cancel_maximum = D(0)
        self.cancel_envelope_breached = False
        self.settling = set()

    @property
    def symbols(self):
        return self._symbols

    @property
    def initial_mode(self):
        return 'best' if self.policy.placement_mode == 'stay_best' else 'fallback'

    def sync_selected(self, symbols):
        """Caller must prove retiring names terminal/flat before removing them.

        Observation never admits an owner. Fresh membership gets no inherited
        evidence, queue anchor, proof or mode; shared breach latch survives.
        """
        names = frozenset(symbols)
        if (self.policy.scope != 'all_selected' or len(names) > MAX_ACTIVE_MARKETS
                or any(not isinstance(s, str) or not s or s != s.strip().upper() for s in names)):
            raise ValueError('invalid selected adaptive scope')
        for s in self.symbols - names:
            for mapping in (self.markets, self.evidence, self.modes, self.cooldown_at,
                            self.disabled, self.reasons, self.cancel_sequence):
                mapping.pop(s, None)
            self.proof_required.discard(s)
            self.settling.discard(s)
        for s in names - self.symbols:
            self.modes[s] = self.initial_mode
            if self.policy.placement_mode == 'stay_best':
                self.proof_required.add(s)
        self._symbols = names

    def invalidate(self, symbol):
        if symbol not in self.symbols:
            return
        super().invalidate(symbol)
        self.evidence.pop(symbol, None)
        self.settling.add(symbol)

    def canceled(self, symbol, at):
        super().canceled(symbol, at)
        self.settling.add(symbol)

    def observe_book(self, book, *, generation, at, orders=()):
        prior = self.markets.get(book.symbol)
        prices = dict(prior.prices) if prior else {}
        depths = dict(prior.depths) if prior else {}
        super().observe_book(book, generation=generation, at=at, orders=orders)
        m = self.markets.get(book.symbol)
        if m is None:
            return
        e = self.evidence.get(book.symbol)
        if e is None or e.generation != generation:
            e = self.evidence[book.symbol] = Evidence(generation, at)
            prices, depths = {}, {}
        for o in Outcome:
            if prices.get(o) == m.prices.get(o):
                e.peak_drop[o] = max(e.peak_drop[o], depths[o] - m.depths[o])

    def observe_trade(self, frame, *, generation, at):
        super().observe_trade(frame, generation=generation, at=at)
        self._peaks(str(frame.get('s', '')).upper(), at)

    def _peaks(self, symbol, now):
        m, e = self.markets.get(symbol), self.evidence.get(symbol)
        if m is None or e is None or (m.flow_clock is not None and now < m.flow_clock):
            return
        flow = m.sell_flow(now)
        for o in Outcome:
            e.peak_flow[o] = max(e.peak_flow[o], flow[o])

    def record_cancel(self, elapsed, *, succeeded):
        value = D(str(elapsed))
        nonnegative(value)
        self.cancel_maximum = max(self.cancel_maximum, value)
        # A rolling percentile can never erase a bad tail. Policy re-review is
        # required after a breach; baseline quoting still has normal safeguards.
        self.cancel_envelope_breached |= not succeeded or value > self.policy.limits.cancel_budget_seconds

    def calibration(self, symbol, outcome):
        e, p = self.evidence[symbol], self.policy.limits
        # Sum may double-count executions reflected in depth. It is a cautious
        # scenario, not a probabilistic bound on future arrivals/cancellations.
        burst = e.peak_flow[outcome] + e.peak_drop[outcome]
        warning = p.buffer_contracts + p.burst_multiplier * burst
        return QueueCalibration(warning * p.depth_multiplier, warning,
            p.minimum_requeue_gain, max(D(1), e.peak_flow[outcome] * p.burst_multiplier),
            p.refresh_cooldown_seconds, p.expires_at, p.evidence_reference)

    def desired_mode(self, symbol, *, now, generation, safe):
        aware(now)
        if symbol not in self.symbols:
            return 'fallback'
        if self.policy.placement_mode == 'stay_best':
            # Placement is an owner policy, never inferred from depth or flow.
            # Advice and the orchestrator independently enforce safety/proof.
            self.reasons[symbol] = 'stay_best_owner_policy'
            return 'best'
        m, e, p = self.markets.get(symbol), self.evidence.get(symbol), self.policy.limits
        reason = None
        if (m is None or e is None or m.generation != generation or e.generation != generation
                or symbol in self.disabled or not safe):
            reason = 'adaptive_evidence_unavailable'
        elif self.policy.expired(now) or self.cancel_envelope_breached:
            reason = 'adaptive_envelope_expired_or_cancel_breached'
        else:
            if (e.last_check is None or now < e.last_check
                    or D(str((now - e.last_check).total_seconds())) > p.observation_gap_seconds):
                e.since, e.good_since = now, None
            e.last_check = now
            self._peaks(symbol, now)
            if (now < m.observed_at or (m.flow_clock is not None and now < m.flow_clock)
                    or (m.uncertain_until is not None and now < m.uncertain_until)
                    or D(str((now - e.since).total_seconds())) < p.warmup_seconds
                    or (now - m.started_at).total_seconds() < 60):
                reason = 'adaptive_continuous_warmup_required'
            elif any(m.depths[o] < self.calibration(symbol, o).minimum_depth for o in Outcome):
                reason = 'adaptive_depth_below_burst_scenario'
        if reason is not None:
            if e is not None:
                e.good_since = None
                if not safe:
                    e.since, e.last_check = now, now
            self.reasons[symbol] = reason
            return 'fallback'
        if e.good_since is None:
            e.good_since = now
        if self.modes[symbol] != 'best' and D(str((now - e.good_since).total_seconds())) < p.promotion_seconds:
            self.reasons[symbol] = 'adaptive_promotion_hysteresis'
            return 'fallback'
        self.reasons[symbol] = 'adaptive_scenario_pass_not_queue_proof'
        return 'best'

    def advice(self, symbol, *, now, generation, orders, safe, allow_entry=False):
        if self.policy.placement_mode == 'stay_best':
            return self._stay_best_advice(symbol, now=now, generation=generation,
                orders=orders, safe=safe, allow_entry=allow_entry)
        if self.desired_mode(symbol, now=now, generation=generation, safe=safe) != 'best':
            return 'withdraw'
        return super().advice(symbol, now=now, generation=generation, orders=orders,
                              safe=safe, allow_entry=allow_entry)

    def _stay_best_advice(self, symbol, *, now, generation, orders, safe, allow_entry):
        aware(now)
        if symbol not in self.symbols:
            return 'outside_pilot'
        m = self.markets.get(symbol)
        if (not safe or m is None or m.generation != generation
                or symbol in self.disabled or now < m.observed_at):
            self.reasons[symbol] = 'canonical_book_or_account_unavailable'
            return 'withdraw'
        working = [o for o in orders if o.symbol == symbol and o.working]
        if ((working and symbol in self.proof_required)
                or len(working) > 2 or len({o.outcome for o in working}) != len(working)
                or any(o.side is not Side.BUY or o.cumulative_filled
                    or o.remaining_quantity != D(15) or o.original_quantity != D(15) for o in working)):
            self.reasons[symbol] = 'fill_or_invalid_pair'
            return 'withdraw'
        since = (D(str((now-self.cooldown_at[symbol]).total_seconds()))
                 if symbol in self.cooldown_at else self.policy.limits.refresh_cooldown_seconds)
        if not working:
            if (symbol in self.proof_required
                    or m.sequence <= self.cancel_sequence.get(symbol, -1)
                    or since < self.policy.limits.refresh_cooldown_seconds):
                self.reasons[symbol] = 'terminal_flat_depth_or_cooldown_required'
                return 'wait'
        # Price tracking is mandatory even when optional queue telemetry expires.
        if any(o.price != m.prices[o.outcome] for o in working):
            self.reasons[symbol] = 'best_price_changed'
            return 'withdraw'
        optional_ready = (not allow_entry and len(working) == 2
            and now < self.policy.limits.expires_at and not self.cancel_envelope_breached
            and since >= self.policy.limits.refresh_cooldown_seconds
            and (now-m.started_at).total_seconds() >= float(self.policy.limits.warmup_seconds)
            and (m.uncertain_until is None or now >= m.uncertain_until)
            and (m.flow_clock is None or now >= m.flow_clock)
            and symbol in self.evidence)
        if optional_ready:
            self._peaks(symbol, now)
            for order in working:
                a = m.anchors.get(order.outcome)
                # Missing queue estimates never pretend to be queue protection.
                if a is None or a.client_id != order.client_order_id:
                    continue
                ahead = a.scenario.ahead_scenario
                gain = max(D(0), m.depths[order.outcome]-ahead)
                if (ahead <= self.calibration(symbol, order.outcome).warning_ahead
                        and gain >= self.policy.limits.minimum_requeue_gain):
                    self.reasons[symbol] = 'estimated_queue_refresh_benefit'
                    return 'withdraw'
        self.reasons[symbol] = 'stay_best_accept_fill_risk'
        return 'allow' if allow_entry else 'keep'

    def report(self):
        return {**super().report(), 'adaptive': True, 'scope': self.policy.scope, 'modes': dict(self.modes),
            'placement_mode': self.policy.placement_mode,
            'queue_settings_expiry_affects_placement': self.policy.placement_mode != 'stay_best',
            'cancel_maximum_seconds': str(self.cancel_maximum),
            'cancel_envelope_breached': self.cancel_envelope_breached,
            'thresholds_are_scenarios': True,
            'evidence': {s: {'since': e.since.isoformat(),
                'outcomes': {o.value: {'peak_sell_flow_60s': str(e.peak_flow[o]),
                    'peak_depth_drop': str(e.peak_drop[o]),
                    'minimum_depth': str(self.calibration(s, o).minimum_depth),
                    'warning_ahead': str(self.calibration(s, o).warning_ahead)} for o in Outcome}}
                for s, e in self.evidence.items()}}
