class RequestQuotaError(RuntimeError):
    """Raised when a local task or work budget is exhausted."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "rate_limited",
        reason: str = "task_rate",
        retry_after_seconds: int | None = None,
    ) -> None:
        self.code = code
        self.reason = reason
        self.retry_after_seconds = retry_after_seconds
        super().__init__(message)
