from typing import Any


class WorkflowWaiting(Exception):
    """A finite decision or external prerequisite is needed before the next stage."""

    def __init__(
        self,
        code: str,
        message: str,
        choices: dict,
        decision_owner: str = "MODEL",
        purpose: str = "GENERAL_DECISION",
        evidence=None,
    ):
        if decision_owner not in {"MODEL", "USER"}:
            raise ValueError(f"Unsupported decision owner: {decision_owner}")
        self.code = code
        self.message = message
        self.choices = choices
        self.decision_owner = decision_owner
        self.purpose = purpose
        self.evidence = evidence or []
        super().__init__(message)


class ExecutorError(RuntimeError):
    def __init__(self, code: str, message: str, evidence: list[Any] | None = None):
        super().__init__(message)
        self.code = code
        self.evidence = evidence or []
