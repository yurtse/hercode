class ExecutionError(RuntimeError):
    """A deterministic factory execution or policy failure."""


class QualityGateError(ExecutionError):
    """A deterministic gate failure with the complete evidence collected so far."""

    def __init__(self, message: str, evidence: list[dict]):
        super().__init__(message)
        self.evidence = evidence
