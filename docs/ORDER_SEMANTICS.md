# Entry and exit semantics

Gemini and Polymarket US must not share an order-side adapter. Their wire models differ.

| Strategy action | Gemini wire order | Full-fill signed-position delta |
| --- | --- | ---: |
| Enter YES (long buy) | `BUY` + `YES` | `+quantity` |
| Exit YES (long sell) | `SELL` + `YES` | `-quantity` |
| Enter NO (short buy) | `BUY` + `NO` | `-quantity` |
| Exit NO (short sell) | `SELL` + `NO` | `+quantity` |

Gemini uses independent `side` and `eventOutcome` fields. Public depth is normalized into YES-price space, but an order price is expressed in the selected outcome's native price: YES uses the YES price, while NO uses `1 - YES price`.

The production rules are stricter than the four wire combinations:

- Reward entry can only create an `ENTRY` intent and send `BUY YES` or `BUY NO`.
- Exit can only create an `EXIT` intent and send `SELL YES` for a positive authenticated position or `SELL NO` for a negative authenticated position.
- Exit quantity must exactly equal the absolute authenticated position. A mismatched outcome, side, or quantity fails before network send.
- A fill is taken from order events and reconciled against the signed position. Acceptance, cancellation, or requested quantity alone never proves a fill.

The Polymarket US reference bot intentionally does something different. Its strategy-side adapter sends only `ORDER_INTENT_BUY_LONG` for the bid/YES direction and `ORDER_INTENT_BUY_SHORT` for the offer/NO direction, including reductions. That venue nets a buy of the opposite outcome against existing inventory. The Polymarket `SELL_LONG`/`SELL_SHORT` price-frame behavior previously inverted a reduction price and added risk, so that workaround must not be copied into Gemini.

The September 15 Gemini commissioning tests confirmed both paths live: 10-contract `BUY YES` followed by `SELL YES`, and 10-contract `BUY NO` followed by `SELL NO`. All four orders filled, authenticated reconciliation ended globally flat, and no active orders remained. The fully filled NO IOC appeared in history with terminal status `cancelled`, which also confirms that fill quantity must come from execution data rather than status.
