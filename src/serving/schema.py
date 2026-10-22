"""Transaction event schema (v1) shared by the API and the Kafka consumer.

Field names match the IEEE-CIS columns so the batch and online feature code read the same
names. The schema deliberately has no label (``isFraud``) and no ``D1``: unknown fields are
rejected, so a client cannot leak the outcome or experimental inputs into scoring.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

EVENT_SCHEMA_VERSION = 1

# Upper bounds keep every accepted event scoreable and storable. Out-of-range values used to
# pass validation and then fail deterministically in scoring (a Kafka poison message):
# the feature path carries IDs through float64 (exact only up to 2**53), and the history
# index packs (entity, time) into one int64 key.
MAX_SAFE_INT = 2**53 - 1
MAX_TRANSACTION_DT = 2**40  # seconds (~34,800 years); also rejects millisecond timestamps

Text = str | None
Number = float | None


class TransactionEvent(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, str_max_length=128)

    TransactionID: int = Field(ge=0, le=MAX_SAFE_INT)
    TransactionDT: int = Field(
        ge=0, le=MAX_TRANSACTION_DT, description="Seconds offset (IEEE-CIS relative time)"
    )
    TransactionAmt: float = Field(ge=0)
    ProductCD: Text = None
    card1: int | None = Field(default=None, ge=-MAX_SAFE_INT, le=MAX_SAFE_INT)
    card2: Number = None
    card3: Number = None
    card4: Text = None
    card5: Number = None
    card6: Text = None
    addr1: Number = None
    addr2: Number = None
    dist1: Number = None
    P_emaildomain: Text = None
    R_emaildomain: Text = None
    DeviceType: Text = None
    DeviceInfo: Text = None
    has_identity: bool = False

    @field_validator(
        "ProductCD",
        "card4",
        "card6",
        "P_emaildomain",
        "R_emaildomain",
        "DeviceType",
        "DeviceInfo",
    )
    @classmethod
    def _no_nul(cls, value: str | None) -> str | None:
        # PostgreSQL JSONB cannot store U+0000, so such an event could never be persisted.
        if value is not None and "\x00" in value:
            raise ValueError("text fields must not contain NUL characters")
        return value

    @model_validator(mode="after")
    def _device_requires_identity(self) -> TransactionEvent:
        # In IEEE-CIS, device fields only exist on identity rows.
        if (self.DeviceType is not None or self.DeviceInfo is not None) and not self.has_identity:
            raise ValueError("DeviceType/DeviceInfo require has_identity=true")
        return self

    def to_record(self) -> dict[str, Any]:
        return self.model_dump()


class ScoreResponse(BaseModel):
    transaction_id: int
    score: float
    is_alert: bool
    threshold: float
    model_version: str
    duplicate: bool
    history_rows: int
    latency_ms: float
    history_features: dict[str, float | None] = Field(
        default_factory=dict,
        description="Point-in-time history features used (empty on duplicates)",
    )
