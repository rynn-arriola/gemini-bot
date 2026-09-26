"""Owner-approved production ceilings; transport quotas are unchanged."""
# Policy v8 and earlier are pinned to exactly 40 orders / 20 owners by the
# policy loader; the Sept 25 owner decision raised the v9 ceiling to 60 / 30.
MAX_PENDING_ORDERS = 60
MAX_ACTIVE_MARKETS = MAX_PENDING_ORDERS // 2
LEGACY_PENDING_ORDERS = 40
