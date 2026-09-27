"""Read-only daily operating report for the Gemini rewards bot.

Reads the canonical control journal (SQLite, read-only), the newest runtime
directory's status and rank report, and Gemini's public reward catalog. It
never places orders, never writes to the journal and never prints balances.
Writes ``data/reports/daily-<ET date>.md`` and prints the same text.
"""
from __future__ import annotations

import json
import sqlite3
import sys
import urllib.request
from collections import Counter
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
CONTROL = Path.home() / "GeminiRewardBot" / "control" / "production.sqlite"
REPORTS = ROOT / "data" / "reports"
ET = ZoneInfo("America/New_York")
NEGATIVE_WEEKS_ALERT_DAYS = 14


def newest_runtime() -> Path | None:
    runs = [p for p in (ROOT / "data").glob("production-*") if (p / "status.json").exists()]
    return max(runs, key=lambda p: (p / "status.json").stat().st_mtime) if runs else None


def read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {}


def catalog_pools() -> dict[str, str]:
    rows, offset = [], 0
    while True:
        url = f"https://api.gemini.com/v1/prediction-markets/liquidity-rewards/events?limit=100&offset={offset}"
        with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "report"}), timeout=30) as r:
            page = json.load(r)
        rows += page["events"]
        offset += 100
        if offset >= page["pagination"]["total"]:
            break
    pools = {}
    for row in rows:
        key = str(row.get("pool_id") or "event:" + row["event_ticker"])
        pools[key] = f"{row.get('pool_category_name') or row.get('category')} ${row['daily_pool_usd']}/day"
    return pools


def realized_by_day(db: sqlite3.Connection) -> dict[str, Decimal]:
    """Round-trip realized P&L by the ET date each lifecycle opened (journal replay)."""
    positions: dict[str, list] = {}
    result: dict[str, Decimal] = {}
    for at, symbol, side, outcome, qty, price, fee, cash in db.execute(
            "SELECT event_at,symbol,side,outcome,quantity,price,fee,cash_delta FROM execution_fill ORDER BY event_at,trade_id"):
        q, p, f = Decimal(qty), Decimal(price), Decimal(fee)
        delta = Decimal(cash) if cash else ((-q * p if side == "BUY" else q * p) - f)
        sign = (1 if outcome == "YES" else -1) * (1 if side == "BUY" else -1)
        state = positions.setdefault(symbol, [Decimal(0), Decimal(0), at])
        if state[0] == 0:
            state[1], state[2] = Decimal(0), at
        state[0] += q * sign
        state[1] += delta
        if state[0] == 0:
            day = datetime.fromisoformat(state[2]).astimezone(ET).date().isoformat()
            result[day] = result.get(day, Decimal(0)) + state[1]
    return result


def main() -> int:
    now = datetime.now(timezone.utc)
    today = now.astimezone(ET).date()
    lines = [f"# Gemini bot daily report — {today.isoformat()} (generated {now.astimezone(ET):%H:%M} ET)", ""]
    alerts: list[str] = []

    runtime = newest_runtime()
    status = read_json(runtime / "status.json") if runtime else {}
    supervisor = read_json(runtime / "supervisor-status.json") if runtime else {}
    phase = status.get("phase", "unknown")
    lines += ["## Bot", f"- Runtime: `{runtime.name if runtime else 'none'}`",
              f"- Phase: **{phase}** (activated: {status.get('activated')})",
              f"- Supervisor: {supervisor.get('state', 'n/a')}"]
    if phase in {"halted"} or supervisor.get("state") == "ATTENTION_REQUIRED":
        alerts.append(f"Bot needs attention: phase {phase}, supervisor {supervisor.get('state')}")
    counters = status.get("counters", {})
    lines += [f"- Working orders: {counters.get('pending_orders', 'n/a')}, open positions: {counters.get('open_positions', 'n/a')}", ""]

    db = sqlite3.connect(f"file:{CONTROL}?mode=ro", uri=True)
    rewards_by_day: dict[str, Decimal] = {}
    for source, amount, occurred in db.execute(
            "SELECT source, amount, occurred_at FROM authenticated_cash_adjustment WHERE source IN ('liquidity_reward','maker_rebate')"):
        day = datetime.fromisoformat(occurred).astimezone(ET).date().isoformat()
        rewards_by_day[day] = rewards_by_day.get(day, Decimal(0)) + Decimal(amount)
    trading = realized_by_day(db)
    lines += ["## Money (last 7 days, ET)", "| Day | Rewards + rebates | Trading P&L | Net |", "|---|---:|---:|---:|"]
    net_window = Decimal(0)
    for i in range(6, -1, -1):
        day = (today - timedelta(days=i)).isoformat()
        r, t = rewards_by_day.get(day, Decimal(0)), trading.get(day, Decimal(0))
        lines.append(f"| {day} | {r:.2f} | {t:.2f} | {r + t:.2f} |")
    for i in range(NEGATIVE_WEEKS_ALERT_DAYS):
        day = (today - timedelta(days=i)).isoformat()
        net_window += rewards_by_day.get(day, Decimal(0)) + trading.get(day, Decimal(0))
    lines += ["", f"- Net over the last {NEGATIVE_WEEKS_ALERT_DAYS} days: **{net_window:.2f}**"]
    if net_window < 0 and any(trading) and min(trading) <= (today - timedelta(days=NEGATIVE_WEEKS_ALERT_DAYS)).isoformat():
        alerts.append(f"Net negative over {NEGATIVE_WEEKS_ALERT_DAYS} days ({net_window:.2f}): owner review before continuing")
    fills = db.execute("SELECT COUNT(*) FROM execution_fill WHERE event_at >= ?",
                       ((now - timedelta(days=1)).isoformat(),)).fetchone()[0]
    lines += [f"- Fills in the last 24 hours: {fills}", ""]

    report = read_json(runtime / "rank-report.json") if runtime else {}
    rows = report.get("candidates", [])
    statuses = Counter((r.get("uptime_ledger") or {}).get("status", "n/a") for r in rows)
    eligible = sum(1 for r in rows if r.get("eligible_now"))
    lines += ["## Markets", f"- Watched: {len(rows)}, eligible now: {eligible}",
              f"- 50% status: {dict(statuses)}"]
    mismatches = [r["symbol"] for r in rows
                  if ((r.get("uptime_ledger") or {}).get("previous_window_check") or {}).get("flag_over_3_percent")]
    if mismatches:
        alerts.append(f"Our uptime count differs from Gemini's by >3% on: {', '.join(mismatches[:5])}")
    results = report.get("exchange_reward_results") or []
    if results:
        last = results[-1]
        events = last.get("events", [])
        qualified = sum(1 for e in events if e["snapshot_count"] * 2 >= e["total_snapshots"])
        lines.append(f"- Gemini's last report ({last.get('payout_date')}): {qualified}/{len(events)} events at ≥50%, "
                     f"paid {last.get('paid_reward_usd')}")
    if phase in {"quoting", "ready_paused"} and eligible == 0:
        alerts.append("No eligible markets right now")
    lines.append("")

    try:
        pools = catalog_pools()
        previous_path = REPORTS / "last-catalog.json"
        previous = read_json(previous_path)
        added = sorted(set(pools) - set(previous)) if previous else []
        removed = sorted(set(previous) - set(pools)) if previous else []
        lines += ["## Gemini reward catalog", f"- Pools: {len(pools)} — " + "; ".join(sorted(set(pools.values())))]
        if added or removed:
            alerts.append(f"Reward pools changed: +{[pools[k] for k in added]} -{[previous[k] for k in removed]}")
        REPORTS.mkdir(parents=True, exist_ok=True)
        previous_path.write_text(json.dumps(pools), encoding="utf-8")
    except Exception as exc:  # Reporting only; never fatal.
        lines.append(f"- Catalog unavailable: {type(exc).__name__}")
    for label, pattern in (("current v9", "shadow-2*"), ("two-layer", "shadow-layered-*")):
        shadows = sorted((ROOT / "data").glob(pattern), key=lambda p: p.stat().st_mtime)
        if not shadows:
            continue
        shadow = shadows[-1]
        summary = read_json(shadow / "summary.json")
        lines += ["", f"## Paper run, {label} (virtual orders, no money)",
                  f"- Run: `{shadow.name}`, last update {summary.get('updated_at')}, ends {summary.get('ends_at')}",
                  f"- Now: {summary.get('eligible_pairs')} eligible, {summary.get('held_markets')} held "
                  f"({summary.get('anchors_now')} anchors), ${summary.get('reserved_usd')} virtually reserved",
                  f"- Totals: {summary.get('fills')} virtual fills, virtual trading P&L {summary.get('trading_pnl')}, "
                  f"{summary.get('repositions')} repositions, {summary.get('minutes')} minutes measured",
                  f"- This window: {summary.get('events_with_coverage_current_window')} events covered, "
                  f"{summary.get('score_units_current_window')} score units"]
        windows_path = shadow / "windows.jsonl"
        if windows_path.exists():
            for raw in windows_path.read_text(encoding="utf-8").splitlines()[-3:]:
                row = json.loads(raw)
                lines.append(f"- Window from {row['window_start']}: {row['qualified_events']} events would have "
                             f"reached 50% ({row.get('qualified_score_units', 'n/a')} score units); "
                             f"rough reward estimate ${row['rough_reward_estimate_usd']}")
        try:
            age = now - datetime.fromisoformat(str(summary.get("updated_at")))
            if age > timedelta(minutes=15) and now < datetime.fromisoformat(str(summary.get("ends_at"))):
                alerts.append("Paper run has not updated for over 15 minutes")
        except (TypeError, ValueError):
            pass
    lines += ["", "## Alerts"] + ([f"- ⚠️ {a}" for a in alerts] or ["- None"])

    text = "\n".join(lines) + "\n"
    REPORTS.mkdir(parents=True, exist_ok=True)
    (REPORTS / f"daily-{today.isoformat()}.md").write_text(text, encoding="utf-8")
    print(text)
    return 1 if alerts else 0


if __name__ == "__main__":
    sys.exit(main())
