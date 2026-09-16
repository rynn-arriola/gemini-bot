"""Evidence blockers are not transport failures or permission to trade."""


class ReconciliationRequired(RuntimeError):
    """May wait read-only before activation; never authorizes a live retry."""


class ExternalOrderPresent(ReconciliationRequired):
    pass


class UnclassifiedCash(ReconciliationRequired):
    pass


class UnclassifiedPositions(ReconciliationRequired):
    pass


class InventoryMarketUnavailable(ReconciliationRequired):
    """Exact held inventory is absent from the current subscribable catalog.

    V7 may wait and repeat the complete read-only startup proof. The exception
    never authorizes stale-market exit, synthetic contract metadata, permit
    consumption, or post-activation recovery.
    """
