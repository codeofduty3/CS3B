"""Pydantic contracts: one input and one output model per tool.
Inputs forbid extra fields, so a model cannot smuggle in an argument."""
from typing import Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field

Decision = Literal["executed", "requested", "blocked", "failed"]


class In(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Scoped(In):
    scope: str = Field(description="'public' or the session's active mandate id")


# ---------------------------------------------------------------- inputs
class ListMandatesIn(In):
    pass


class BdcHoldingsIn(Scoped):
    bdc: Optional[str] = Field(None, description="exact BDC name, as the panel spells it")


class BdcSignalsIn(Scoped):
    bdc: Optional[str] = None


class GetFilingIn(In):
    adsh: str = Field(pattern=r"^\d{10}-\d{2}-\d{6}$", description="accession number")


class MandateProfileIn(Scoped):
    pass


class WatchlistReadIn(Scoped):
    pass


class MemoryReadIn(Scoped):
    query: str = Field(min_length=1)
    k: int = Field(5, ge=1, le=20)


class MemoryProposeWriteIn(Scoped):
    content: str = Field(min_length=1, max_length=500)
    message_id: Optional[str] = Field(None, description="the authenticated message this came from, if any")


class WatchlistWriteIn(Scoped):
    bdc: str
    reason: str = Field(min_length=1, max_length=200)
    approval_token: Optional[str] = None


class DraftEmailIn(Scoped):
    to: list[str] = Field(min_length=1)
    subject: str = Field(min_length=1)
    body: str = Field(min_length=1)
    attachments: list[str] = []


class RequestApprovalIn(In):
    item_id: str = Field(description="a draft_id or a watchlist request_id")


class SendEmailIn(Scoped):
    draft_id: str
    approval_token: str


# ---------------------------------------------------------------- outputs
class MandateRef(BaseModel):
    mandate_id: str
    active: bool
    client: Optional[str] = None          # named only for the ACTIVE mandate


class ListMandatesOut(BaseModel):
    mandates: list[MandateRef]


class Holding(BaseModel):
    adsh: str
    name: str
    identifier: str
    fair_value: Optional[float]
    principal: Optional[float]
    pik_rate: Optional[float]
    source_rows: str


class HoldingsOut(BaseModel):
    holdings: list[Holding]


class Signal(BaseModel):
    adsh: str
    name: str
    rule: str
    identifier: str
    row: str
    prior_row: str
    before: Union[float, str, None]
    after: Union[float, str, None]


class SignalsOut(BaseModel):
    signals: list[Signal]


class FilingOut(BaseModel):
    adsh: str
    cik: str
    name: str
    form: str
    period: str
    filed: str
    url: str
    rows: int


class MandateProfileOut(BaseModel):
    mandate_id: str
    client: str
    positions: dict[str, float]


class WatchlistEntry(BaseModel):
    bdc: str
    reason: str
    added: str


class WatchlistOut(BaseModel):
    entries: list[WatchlistEntry]


class MemoryHit(BaseModel):
    text: str
    source: str


class MemoryReadOut(BaseModel):
    hits: list[MemoryHit]


class MemoryWriteOut(BaseModel):
    status: Literal["written", "proposed"]


class WatchlistWriteOut(BaseModel):
    status: Literal["requested", "written"]
    request_id: Optional[str] = None
    entry_id: Optional[str] = None


class DraftOut(BaseModel):
    draft_id: str
    payload_hash: str


class ApprovalRequestOut(BaseModel):
    item_id: str
    status: Literal["pending"]
    payload_hash: str


class SendOut(BaseModel):
    outbox_message_id: str
    status: Literal["sent_to_mock_outbox"]
