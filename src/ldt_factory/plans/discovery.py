from __future__ import annotations

import datetime as dt
import json
import logging
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urljoin

from ..domains._api_cache import atomic_write_json
from ..logging_utils import logged_action
from .config import DevelopmentPlanConfig
from .exa import build_search_request
from .models import AreaSelection, PlanCandidate
from .registry import AdminArea
from .scoring import (
    canonicalize_url,
    deduplicate_candidates,
    normalize_status,
    parse_year,
    score_candidate,
    select_area_candidate,
    source_domain,
    source_tier,
)


DISCOVERY_SCHEMA_VERSION = 1


class SearchClient(Protocol):
    def search(self, payload: dict[str, Any]) -> dict[str, Any]: ...


def _summary(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {}


def _document_link(result: dict[str, Any], summary: dict[str, Any]) -> str:
    landing = canonicalize_url(str(result.get("url") or ""))
    direct = canonicalize_url(str(summary.get("direct_document_url") or ""))
    if direct:
        return direct
    if landing.lower().split("?", 1)[0].endswith((".pdf", ".doc", ".docx")):
        return landing
    links = result.get("extras", {}).get("links", []) if isinstance(result.get("extras"), dict) else []
    for value in links if isinstance(links, list) else []:
        resolved = canonicalize_url(urljoin(landing, str(value)))
        if resolved.lower().split("?", 1)[0].endswith((".pdf", ".doc", ".docx")):
            return resolved
    return ""


def candidates_from_response(
    config: DevelopmentPlanConfig,
    area: AdminArea,
    *,
    pass_number: int,
    request: dict[str, Any],
    response: dict[str, Any],
    current_year: int,
) -> list[PlanCandidate]:
    results = response.get("results", [])
    if not isinstance(results, list):
        raise ValueError("Exa search response results must be a list")
    request_id = str(response.get("requestId") or "")
    candidates: list[PlanCandidate] = []
    for rank, result in enumerate(results, start=1):
        if not isinstance(result, dict):
            continue
        landing = canonicalize_url(str(result.get("url") or ""))
        if not landing:
            continue
        structured = _summary(result.get("summary"))
        highlights = result.get("highlights", [])
        evidence = [str(item).strip() for item in highlights if str(item).strip()] if isinstance(highlights, list) else []
        title = str(result.get("title") or "").strip()
        combined = " ".join([title, *evidence, json.dumps(structured, ensure_ascii=False)])
        status = normalize_status(str(structured.get("status") or "unknown"), combined, config)
        candidate = PlanCandidate(
            admin2_id=area.admin2_id,
            admin2_name=area.name,
            parent=area.parent,
            storage_slug=area.storage_slug,
            pass_number=pass_number,
            search_rank=rank,
            query=str(request["query"]),
            title=title,
            landing_url=landing,
            document_url=_document_link(result, structured),
            published_date=str(result.get("publishedDate") or ""),
            request_id=request_id,
            source_domain=source_domain(landing),
            source_tier=source_tier(config, landing),
            jurisdiction_match=structured.get("jurisdiction_match") is True,
            document_type=(
                "development_plan" if structured.get("is_development_plan") is True else "other_or_unknown"
            ),
            document_status=status,
            is_full_document=structured.get("is_full_document") is True,
            start_year=parse_year(structured.get("start_year")),
            end_year=parse_year(structured.get("end_year")),
            adoption_date=str(structured.get("adoption_date") or "").strip(),
            issuing_authority=str(structured.get("issuing_authority") or "").strip(),
            evidence=evidence,
        )
        candidates.append(score_candidate(candidate, config, current_year=current_year))
    return candidates


def _area_path(config: DevelopmentPlanConfig, run_id: str, area: AdminArea) -> Path:
    return config.run_dir(run_id) / "candidates" / f"{area.admin2_id}.json"


def _load_area_record(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or payload.get("schema_version") != DISCOVERY_SCHEMA_VERSION:
        raise ValueError(f"Unsupported discovery record: {path}")
    return payload


def discover_area(
    config: DevelopmentPlanConfig,
    area: AdminArea,
    *,
    run_id: str,
    client: SearchClient,
    logger: logging.Logger,
    force: bool = False,
    today: dt.date | None = None,
) -> AreaSelection:
    today = today or dt.date.today()
    path = _area_path(config, run_id, area)
    candidates: list[PlanCandidate] = []
    completed_passes = 0
    pass_errors: list[str] = []
    requests: list[dict[str, Any]] = []
    costs: list[dict[str, Any]] = []
    if path.is_file() and not force:
        cached = _load_area_record(path)
        candidates = [PlanCandidate.from_dict(value) for value in cached.get("candidates", [])]
        completed_passes = int(cached.get("completed_passes", 0))
        pass_errors = list(cached.get("pass_errors", []))
        requests = list(cached.get("requests", []))
        costs = list(cached.get("costs", []))

    minimum_passes = int(config.search.get("minimum_passes", 2))
    max_passes = int(config.search["max_passes"])
    for pass_number in range(completed_passes + 1, max_passes + 1):
        request = build_search_request(config, area, pass_number)
        requests.append(request)
        try:
            with logged_action(
                logger,
                "development_plan_search",
                domain="development_plans",
                phase=f"pass_{pass_number}",
                task_id=area.admin2_id,
            ):
                response = client.search(request)
            cost = response.get("costDollars", {})
            if isinstance(cost, dict):
                costs.append(
                    {
                        "pass_number": pass_number,
                        "request_id": str(response.get("requestId") or ""),
                        "total": float(cost.get("total", 0.0) or 0.0),
                    }
                )
            candidates.extend(
                candidates_from_response(
                    config,
                    area,
                    pass_number=pass_number,
                    request=request,
                    response=response,
                    current_year=today.year,
                )
            )
        except Exception as error:
            message = f"pass_{pass_number}:{type(error).__name__}:{error}"
            pass_errors.append(message)
            logger.warning(
                "development-plan search pass failed area=%s pass=%d error=%s",
                area.admin2_id,
                pass_number,
                error,
                extra={
                    "action": "development_plan_search_error",
                    "domain": "development_plans",
                    "phase": f"pass_{pass_number}",
                    "task_id": area.admin2_id,
                },
            )
        completed_passes = pass_number
        ranked = deduplicate_candidates(candidates)
        record = {
            "schema_version": DISCOVERY_SCHEMA_VERSION,
            "run_id": run_id,
            "area": {
                "admin2_id": area.admin2_id,
                "name": area.name,
                "parent": area.parent,
                "storage_slug": area.storage_slug,
            },
            "completed_passes": completed_passes,
            "pass_errors": pass_errors,
            "requests": requests,
            "costs": costs,
            "candidates": [candidate.to_dict() for candidate in ranked],
            "updated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        }
        atomic_write_json(path, record)
        selection = select_area_candidate(
            area,
            ranked,
            config,
            completed_passes=completed_passes,
            pass_errors=pass_errors,
        )
        if completed_passes >= minimum_passes and selection.proposed_decision == "AUTO_ACCEPT":
            return selection

    return select_area_candidate(
        area,
        deduplicate_candidates(candidates),
        config,
        completed_passes=completed_passes,
        pass_errors=pass_errors,
    )


def load_area_selection(
    config: DevelopmentPlanConfig,
    run_id: str,
    area: AdminArea,
) -> tuple[AreaSelection, list[PlanCandidate]]:
    record = _load_area_record(_area_path(config, run_id, area))
    candidates = [PlanCandidate.from_dict(value) for value in record.get("candidates", [])]
    selection = select_area_candidate(
        area,
        candidates,
        config,
        completed_passes=int(record.get("completed_passes", 0)),
        pass_errors=list(record.get("pass_errors", [])),
    )
    return selection, candidates


def discover_all(
    config: DevelopmentPlanConfig,
    areas: list[AdminArea],
    *,
    run_id: str,
    client: SearchClient,
    logger: logging.Logger,
    force: bool = False,
    limit: int | None = None,
) -> list[AreaSelection]:
    config.prepare_run(run_id)
    selected_areas = areas[:limit] if limit is not None else areas
    selections = [
        discover_area(
            config,
            area,
            run_id=run_id,
            client=client,
            logger=logger,
            force=force,
        )
        for area in selected_areas
    ]
    manifest = {
        "schema_version": DISCOVERY_SCHEMA_VERSION,
        "run_id": run_id,
        "country": config.iso3,
        "config_path": str(config.path),
        "requested_count": len(selected_areas),
        "status_counts": {
            status: sum(selection.proposed_decision == status for selection in selections)
            for status in ("AUTO_ACCEPT", "REVIEW", "MISSING")
        },
        "completed_at": dt.datetime.now(dt.timezone.utc).isoformat(),
    }
    atomic_write_json(config.run_dir(run_id) / "discovery.json", manifest)
    return selections
