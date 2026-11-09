"""Typed tool errors. Each carries a stable code (what the model sees) and
the decision recorded for the action ("blocked" for refusals)."""


class ToolFailure(Exception):
    code = "TOOL_FAILURE"
    decision = "failed"

    def __init__(self, detail, rule_id=None):
        super().__init__(detail)
        self.detail, self.rule_id = detail, rule_id

    def as_dict(self):
        return {"error": self.code, "rule_id": self.rule_id, "detail": self.detail}


class BadInput(ToolFailure):
    """Arguments failed the tool's input schema."""
    code = "BAD_INPUT"


class NotFound(ToolFailure):
    """A filing, BDC, draft or request that does not exist."""
    code = "NOT_FOUND"


class ScopeDenied(ToolFailure):
    """Scope outside the session's mandate (R-SCOPE)."""
    code = "SCOPE_DENIED"
    decision = "blocked"


class PolicyBlocked(ToolFailure):
    """A standing rule fired (R-BORROWER, R-NOT-CONTACT, R-OTHER-MANDATE, R-RESTRICTED)."""
    code = "POLICY_BLOCKED"
    decision = "blocked"


class ApprovalInvalid(ToolFailure):
    """Missing, malformed, expired or payload-changed approval token."""
    code = "APPROVAL_INVALID"
    decision = "blocked"
