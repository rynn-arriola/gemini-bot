"""Exact execution recovery, joined to authenticated terminal order history."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
from .account_state import OwnedOrder
from .cash_accounting import fill_cash_delta, netted_quantity
from .domain import Side, Outcome
from .execution import OrderPurpose


def timestamp(raw):
    result = datetime.fromisoformat(str(raw).replace('Z', '+00:00'))
    if result.tzinfo is None:
        raise ValueError('history timestamp must be aware')
    return result.astimezone(timezone.utc)


def import_terminal_trades(account, rows, trades, *, now, allow_external_reductions=False):
    """External SELLs require operator authority and can only reduce owned basis.

    Fresh REST/WS cash and position proof is still required before any entry.
    No fill, fee, baseline, deadline extension or execution retry is inferred.
    """
    journal = account.journal
    if account.expected_positions is None or account.expected_confirmed_usd is None:
        raise RuntimeError('execution recovery requires a durable accounting baseline')
    trade_index = {}
    for trade in trades:
        tid = str(trade.get('tid', ''))
        if not tid or tid in trade_index:
            raise RuntimeError('missing or duplicate authenticated trade identity')
        trade_index[tid] = trade
    plans, aggregates, seeded, seen_orders = [], [], [], set()
    for row in rows:
        oid, client = str(row.get('orderId', '')), str(row.get('clientOrderId', ''))
        intent = journal.connection.execute(
            'SELECT symbol,outcome,side,purpose,price,quantity,server_order_id FROM order_intent WHERE client_order_id=?', (client,),
        ).fetchone()
        if intent is None and not allow_external_reductions:
            continue
        executed = Decimal(str(row.get('filledQuantity', '0')))
        if not executed.is_finite() or executed < 0:
            raise RuntimeError('invalid historical executed quantity')
        if not executed:
            continue
        if oid in seen_orders:
            raise RuntimeError('duplicate history order identity')
        seen_orders.add(oid)
        status = str(row.get('status', '')).upper().replace('CANCELLED', 'CANCELED')
        symbol, outcome, side = (str(row.get(key, '')).upper() for key in ('symbol','outcome','side'))
        original, limit = Decimal(str(row.get('quantity'))), Decimal(str(row.get('price')))
        created, terminal = timestamp(row.get('createdAt')), timestamp(row.get('updatedAt'))
        if (not oid or not client or side not in {'BUY','SELL'} or outcome not in {'YES','NO'}
                or status not in {'FILLED','CANCELED'} or not original.is_finite()
                or not 0 < executed <= original or not limit.is_finite() or not 0 < limit < 1
                or not created <= terminal <= now):
            raise RuntimeError('execution evidence requires exact terminal order history')
        if intent is not None and ((symbol,outcome,side) != intent[:3] or original != Decimal(intent[5])
                                  or limit != Decimal(intent[4]) or str(intent[6]) != oid):
            raise RuntimeError('historical execution identity disagrees with owned intent')
        if intent is None and side != 'SELL':
            raise RuntimeError('external history may only reduce known inventory')
        matched = []
        for tid, trade in trade_index.items():
            if str(trade.get('order_id','')) != oid:
                continue
            quantity, price, fee = (Decimal(str(trade.get(key))) for key in ('amount','price','fee_amount'))
            ms = int(trade.get('timestampms',0))
            event_at = datetime.fromtimestamp(ms / 1000, timezone.utc)
            if (str(trade.get('client_order_id','')) != client or str(trade.get('symbol','')).upper() != symbol
                    or str(trade.get('type','')).upper() != side or trade.get('fee_currency') != 'USD'
                    or trade.get('break') not in {None,'trade','correct'}
                    or not all(v.is_finite() for v in (quantity,price,fee))
                    or quantity <= 0 or not 0 < price < 1 or fee < 0 or ms <= 0
                    or not created <= event_at <= terminal
                    or (side == 'BUY' and price > limit) or (side == 'SELL' and price < limit)):
                raise RuntimeError('authenticated trade disagrees with terminal order')
            matched.append((
                tid, oid, client, symbol, side, outcome, quantity, price, fee,
                event_at, intent is None,
                OrderPurpose.EXIT.value if intent is None else str(intent[3]),
            ))
        if sum((t[6] for t in matched),Decimal(0)) != executed:
            raise RuntimeError('terminal execution evidence is incomplete')
        stored = journal.connection.execute(
            'SELECT trade_id,quantity,price,fee,cash_delta FROM execution_fill WHERE order_id=?',
            (oid,),
        ).fetchall()
        old_aggregate = [r for r in stored if str(r[0]) == 'maker-history:' + oid]
        if old_aggregate:
            aggregates.append((oid, old_aggregate[0]))
        for t in matched:
            old = journal.connection.execute(
                'SELECT order_id,client_order_id,symbol,side,outcome,quantity,price,fee FROM execution_fill WHERE trade_id=?', (t[0],),
            ).fetchone()
            if old is not None:
                if old[:5] != t[1:6] or tuple(Decimal(v) for v in old[5:]) != t[6:9]:
                    raise RuntimeError('recorded trade identity or economics changed')
            else:
                plans.append(t)
        actual_ids = {t[0] for t in matched}
        if any(str(r[0]) not in actual_ids and str(r[0]) != 'maker-history:' + oid for r in stored):
            raise RuntimeError('terminal trade list omits recorded execution')
        seeded.append((row, matched, executed))

    positions, cash = dict(account.expected_positions), account.expected_confirmed_usd
    # This incident's provisional aggregate is replaced by exact IDs and fees.
    # Its audit evidence remains. No loss-window high-water is changed.
    for oid, old in aggregates:
        row = next(r for r,_,_ in seeded if str(r['orderId']) == oid)
        symbol, outcome = str(row['symbol']).upper(), str(row['outcome']).upper()
        positions[symbol] = positions.get(symbol,Decimal(0)) - Decimal(old[1]) * (1 if outcome == 'YES' else -1)
        old_cash_delta = (
            Decimal(str(old[4])) if str(old[4])
            else -Decimal(old[1]) * Decimal(old[2]) - Decimal(old[3])
        )
        cash -= old_cash_delta
        if not positions[symbol]: positions.pop(symbol)
    cash_deltas: dict[str, Decimal] = {}
    opened_quantities: dict[str, Decimal] = {}
    for t in sorted(plans,key=lambda t:(t[9],t[0])):
        tid,oid,client,symbol,side,outcome,quantity,price,fee,event_at,external,purpose_value = t
        prior = positions.get(symbol,Decimal(0))
        sign = 1 if outcome == 'YES' else -1
        position_delta = quantity * sign * (1 if side == 'BUY' else -1)
        if (purpose_value == OrderPurpose.EXIT.value
                and (prior == 0 or prior * position_delta >= 0 or abs(position_delta) > abs(prior))):
            raise RuntimeError('recovered reduction exceeds known owned inventory')
        positions[symbol] = prior + position_delta
        if not positions[symbol]: positions.pop(symbol)
        precision = account.quote_asset_precision.get(symbol)
        if precision is None:
            raise RuntimeError('execution recovery lacks quote-asset precision')
        cash_deltas[tid] = fill_cash_delta(
            side=Side(side), outcome=Outcome(outcome), purpose=OrderPurpose(purpose_value),
            quantity=quantity, price=price, fee=fee, quote_asset_precision=precision,
            prior_signed_position=prior,
        )
        opened_quantities[tid] = quantity - netted_quantity(
            side=Side(side), outcome=Outcome(outcome), quantity=quantity,
            prior_signed_position=prior,
        )
        cash += cash_deltas[tid]
    if plans or aggregates:
        with journal.transaction():
            for oid, old in aggregates:
                journal.connection.execute('DELETE FROM execution_fill WHERE trade_id=?',(old[0],))
                journal.connection.execute('DELETE FROM runtime_state WHERE key=?',('maker_history_terminal:' + oid,))
                journal.audit('provisional_maker_aggregate_replaced_by_exact_trades',{'order_id':oid})
            for t in sorted(plans,key=lambda t:(t[9],t[0])):
                tid,oid,client,symbol,side,outcome,quantity,price,fee,event_at,external,purpose_value = t
                journal.record_fill(trade_id=tid,order_id=oid,client_order_id=client,symbol=symbol,
                    side=side,outcome=outcome,quantity=quantity,price=price,fee=fee,
                    cash_delta=cash_deltas[tid],event_at=event_at)
                if purpose_value == OrderPurpose.ENTRY.value and opened_quantities[tid] > 0:
                    deadline=event_at+timedelta(seconds=account.inventory_age_seconds)
                    if symbol in account.expiry:
                        deadline=min(deadline,account.expiry[symbol]-timedelta(seconds=account.exit_before_expiry_seconds))
                    if symbol in account.hard_exit_cutoff: deadline=min(deadline,account.hard_exit_cutoff[symbol])
                    journal.persist_deadline(symbol,outcome,event_at,deadline,quantity)
                journal.audit('authenticated_execution_recovered',{'order_id':oid,'symbol':symbol,'external_reduction':external,'fee_basis':'authenticated_trade'})
            for row,matched,executed in seeded:
                if not journal.knows_client_order_id(str(row['clientOrderId'])):
                    journal.set_runtime_state('external_reduction_terminal:' + str(row['orderId']),json.dumps({
                        'client':str(row['clientOrderId']),'symbol':str(row['symbol']).upper(),
                        'outcome':str(row['outcome']).upper(),'quantity':str(executed),
                        'trades':[t[0] for t in matched],
                    }),updated_at=now)
            journal.set_runtime_state('expected_positions',json.dumps({k:str(v) for k,v in positions.items()}),updated_at=now)
            journal.set_runtime_state('expected_confirmed_usd',str(cash),updated_at=now)
        account.expected_positions, account.expected_confirmed_usd = positions,cash
        account.fill_version += 1
    for row, matched, executed in seeded:
        oid,client,symbol = str(row['orderId']),str(row['clientOrderId']),str(row['symbol']).upper()
        account.missing_execution_orders.discard(oid)
        order=account.reducer.orders.get(oid)
        if order is None:
            order=OwnedOrder(oid,client,symbol,Side(str(row['side']).upper()),Outcome(str(row['outcome']).upper()),
                Decimal(str(row['price'])),Decimal(str(row['quantity'])),Decimal(0),executed,
                str(row['status']).upper().replace('CANCELLED','CANCELED'),int(timestamp(row['updatedAt']).timestamp()*1_000_000_000))
            account.reducer.orders[oid]=order
        order.cumulative_filled,order.remaining_quantity=executed,Decimal(0)
        order.status=str(row['status']).upper().replace('CANCELLED','CANCELED')
        order.seen_trade_ids.update(t[0] for t in matched)
        if account.expected_positions.get(symbol): account.block_entries(symbol,fill=True)
    return len(plans)
