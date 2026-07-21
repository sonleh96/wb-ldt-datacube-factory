from __future__ import annotations

import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .config import DevelopmentPlanConfig
from .models import AreaSelection, PlanCandidate
from .registry import AdminArea


FORMAL_STATUSES = {"adopted", "approved", "final"}
DRAFT_STATUSES = {"draft", "consultation", "citizen_version", "unknown"}


def canonicalize_url(url: str) -> str:
    value = url.strip()
    if not value:
        return ""
    split = urlsplit(value)
    if split.scheme.lower() not in {"http", "https"} or not split.netloc:
        return ""
    query = [
        (key, item)
        for key, item in parse_qsl(split.query, keep_blank_values=True)
        if not key.lower().startswith("utm_") and key.lower() not in {"fbclid", "gclid"}
    ]
    path = re.sub(r"/{2,}", "/", split.path)
    return urlunsplit((split.scheme.lower(), split.netloc.lower(), path, urlencode(query), ""))


def source_domain(url: str) -> str:
    return (urlsplit(url).hostname or "").lower()


def source_tier(config: DevelopmentPlanConfig, landing_url: str) -> str:
    domain = source_domain(landing_url)
    for suffix in config.search.get("official_domain_suffixes", ()):
        normalized = str(suffix).lower().lstrip(".")
        if domain == normalized or domain.endswith(f".{normalized}"):
            return "A"
    return "C" if domain else "D"


def normalize_status(value: str, text: str, config: DevelopmentPlanConfig) -> str:
    normalized = value.casefold().strip().replace(" ", "_").replace("-", "_")
    combined = f"{value} {text}".casefold()
    if "citizen" in combined or "citizens" in combined:
        return "citizen_version"
    if "consult" in combined or any(term.casefold() in combined for term in config.search.get("draft_terms", ())):
        return "draft"
    if "approv" in combined:
        return "approved"
    if "adopt" in combined or "усвој" in combined or "usvoj" in combined:
        return "adopted"
    if "final" in combined or "финал" in combined:
        return "final"
    if any(term.casefold() in combined for term in config.search.get("final_terms", ())):
        return "final"
    if normalized in FORMAL_STATUSES | DRAFT_STATUSES:
        return normalized
    return "unknown"


def parse_year(value: object) -> int | None:
    match = re.search(r"\b(19|20)\d{2}\b", str(value or ""))
    return int(match.group()) if match else None


def score_candidate(
    candidate: PlanCandidate,
    config: DevelopmentPlanConfig,
    *,
    current_year: int,
) -> PlanCandidate:
    reasons: list[str] = []
    score = 0.0
    if candidate.jurisdiction_match:
        score += 0.25
    else:
        reasons.append("jurisdiction_unverified")
    if candidate.document_type == "development_plan":
        score += 0.20
    else:
        reasons.append("wrong_or_unverified_document_type")
    if candidate.is_full_document:
        score += 0.10
    else:
        reasons.append("not_verified_full_document")
    if candidate.document_status in FORMAL_STATUSES:
        score += 0.15
    else:
        reasons.append(f"status_{candidate.document_status}")
    if candidate.source_tier in {"A", "B"}:
        score += 0.15
    else:
        reasons.append(f"source_tier_{candidate.source_tier}")
    current = (
        candidate.start_year is not None
        and candidate.end_year is not None
        and candidate.start_year <= current_year <= candidate.end_year
    )
    if current:
        score += 0.10
    else:
        reasons.append("period_not_current_or_unknown")
    if candidate.document_url:
        score += 0.05
    else:
        reasons.append("no_direct_document_url")

    threshold = float(config.review["auto_accept_threshold"])
    require_adoption = bool(config.review.get("require_formal_adoption_for_auto_accept", True))
    candidate.score = round(score, 4)
    candidate.reason_codes = reasons
    candidate.eligible_for_auto_accept = (
        candidate.score >= threshold
        and candidate.jurisdiction_match
        and candidate.document_type == "development_plan"
        and candidate.is_full_document
        and current
        and candidate.source_tier in {"A", "B"}
        and (candidate.document_status in FORMAL_STATUSES or not require_adoption)
    )
    return candidate


def deduplicate_candidates(candidates: list[PlanCandidate]) -> list[PlanCandidate]:
    selected: dict[str, PlanCandidate] = {}
    for candidate in candidates:
        identity = canonicalize_url(candidate.identity_url)
        if not identity:
            continue
        current = selected.get(identity)
        ordering = (candidate.score, candidate.end_year or 0, -candidate.pass_number, -candidate.search_rank)
        if current is None:
            selected[identity] = candidate
            continue
        current_ordering = (current.score, current.end_year or 0, -current.pass_number, -current.search_rank)
        if ordering > current_ordering:
            candidate.evidence = list(dict.fromkeys([*current.evidence, *candidate.evidence]))
            selected[identity] = candidate
        else:
            current.evidence = list(dict.fromkeys([*current.evidence, *candidate.evidence]))
    return sorted(
        selected.values(),
        key=lambda item: (
            item.eligible_for_auto_accept,
            item.score,
            item.end_year or 0,
            item.adoption_date,
            -item.pass_number,
            -item.search_rank,
        ),
        reverse=True,
    )


def select_area_candidate(
    area: AdminArea,
    candidates: list[PlanCandidate],
    config: DevelopmentPlanConfig,
    *,
    completed_passes: int,
    pass_errors: list[str] | None = None,
) -> AreaSelection:
    ranked = deduplicate_candidates(candidates)
    errors = tuple(pass_errors or ())
    if not ranked:
        return AreaSelection(area, "MISSING", None, ("no_candidates",), completed_passes, errors)
    top = ranked[0]
    runner_score = ranked[1].score if len(ranked) > 1 else 0.0
    margin = top.score - runner_score
    minimum_passes = int(config.search.get("minimum_passes", 2))
    minimum_margin = float(config.review["minimum_margin"])
    if (
        top.eligible_for_auto_accept
        and completed_passes >= minimum_passes
        and not errors
        and margin >= minimum_margin
    ):
        return AreaSelection(area, "AUTO_ACCEPT", top, (), completed_passes)
    reasons = list(top.reason_codes)
    if completed_passes < minimum_passes:
        reasons.append("minimum_search_passes_not_completed")
    if errors:
        reasons.append("search_pass_errors")
    if margin < minimum_margin:
        reasons.append("ambiguous_top_candidates")
    return AreaSelection(area, "REVIEW", top, tuple(dict.fromkeys(reasons)), completed_passes, errors)
