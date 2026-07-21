from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from .registry import AdminArea


@dataclass
class PlanCandidate:
    admin2_id: str
    admin2_name: str
    parent: str
    storage_slug: str
    pass_number: int
    search_rank: int
    query: str
    title: str
    landing_url: str
    document_url: str
    published_date: str
    request_id: str
    source_domain: str
    source_tier: str
    jurisdiction_match: bool
    document_type: str
    document_status: str
    is_full_document: bool
    start_year: int | None
    end_year: int | None
    adoption_date: str
    issuing_authority: str
    evidence: list[str] = field(default_factory=list)
    score: float = 0.0
    eligible_for_auto_accept: bool = False
    reason_codes: list[str] = field(default_factory=list)

    @property
    def identity_url(self) -> str:
        return self.document_url or self.landing_url

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "PlanCandidate":
        payload = dict(value)
        payload["evidence"] = list(payload.get("evidence") or ())
        payload["reason_codes"] = list(payload.get("reason_codes") or ())
        return cls(**payload)


@dataclass(frozen=True)
class AreaSelection:
    area: AdminArea
    proposed_decision: str
    selected: PlanCandidate | None
    reason_codes: tuple[str, ...]
    completed_passes: int
    pass_errors: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "admin2_id": self.area.admin2_id,
            "admin2_name": self.area.name,
            "parent": self.area.parent,
            "storage_slug": self.area.storage_slug,
            "proposed_decision": self.proposed_decision,
            "selected": self.selected.to_dict() if self.selected else None,
            "reason_codes": list(self.reason_codes),
            "completed_passes": self.completed_passes,
            "pass_errors": list(self.pass_errors),
        }
