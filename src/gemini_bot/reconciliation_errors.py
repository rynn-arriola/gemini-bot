"""Evidence blockers are not transport failures or permission to trade."""


class ReconciliationRequired(RuntimeError):
    """May wait read-only before activation; never authorizes a live retry."""


class ExternalOrderPresent(ReconciliationRequired):
    pass


class UnclassifiedCash(ReconciliationRequired):
    pass


class UnclassifiedPositions(ReconciliationRequired):
    pass
