"""User-safe application errors with separate diagnostic context."""


class AfterTraceError(Exception):
    def __init__(
        self,
        message: str,
        *,
        operation: str,
        source: str | None = None,
        detail: str | None = None,
        partial_results_usable: bool = False,
    ) -> None:
        super().__init__(message)
        self.operation = operation
        self.source = source
        self.detail = detail
        self.partial_results_usable = partial_results_usable


class DatabaseError(AfterTraceError):
    """An AfterTrace database could not be opened or initialized."""


class UnsupportedOperationError(AfterTraceError):
    """An operation is intentionally unavailable in this milestone."""
