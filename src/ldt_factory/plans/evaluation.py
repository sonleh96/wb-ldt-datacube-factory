from __future__ import annotations

import csv
import json
import math
from pathlib import Path
from typing import Any

from ..domains._api_cache import atomic_write_json
from .config import DevelopmentPlanConfig
from .discovery import load_area_selection
from .registry import AdminArea
from .scoring import canonicalize_url


def _dcg(relevances: list[int], k: int) -> float:
    return sum(
        (2**relevance - 1) / math.log2(index + 2)
        for index, relevance in enumerate(relevances[:k])
    )


def _load_gold(path: Path) -> dict[str, list[dict[str, Any]]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        required = {"admin2_id", "candidate_url", "relevance"}
        missing = sorted(required - set(reader.fieldnames or ()))
        if missing:
            raise ValueError(f"Gold CSV is missing columns: {', '.join(missing)}")
        rows = list(reader)
    by_area: dict[str, list[dict[str, Any]]] = {}
    seen: set[tuple[str, str]] = set()
    for row_number, row in enumerate(rows, start=2):
        admin2_id = str(row.get("admin2_id") or "").strip()
        url = canonicalize_url(str(row.get("candidate_url") or ""))
        try:
            relevance = int(str(row.get("relevance") or ""))
        except ValueError as error:
            raise ValueError(f"Invalid relevance on gold row {row_number}") from error
        if not admin2_id or not url or relevance not in {0, 1, 2, 3}:
            raise ValueError(f"Invalid gold row {row_number}; relevance must be 0, 1, 2, or 3")
        identity = (admin2_id, url)
        if identity in seen:
            raise ValueError(f"Duplicate gold candidate on row {row_number}: {admin2_id} {url}")
        seen.add(identity)
        normalized = dict(row)
        normalized["candidate_url"] = url
        normalized["relevance"] = relevance
        by_area.setdefault(admin2_id, []).append(normalized)
    return by_area


def _discovery_cost(config: DevelopmentPlanConfig, run_id: str, area: AdminArea) -> float:
    path = config.run_dir(run_id) / "candidates" / f"{area.admin2_id}.json"
    if not path.is_file():
        return 0.0
    payload = json.loads(path.read_text(encoding="utf-8"))
    return sum(float(item.get("total", 0.0) or 0.0) for item in payload.get("costs", []))


def evaluate_run(
    config: DevelopmentPlanConfig,
    run_id: str,
    areas: list[AdminArea],
    *,
    gold_path: Path | None = None,
) -> dict[str, Any]:
    selections = []
    candidates_by_area = {}
    for area in areas:
        try:
            selection, candidates = load_area_selection(config, run_id, area)
        except FileNotFoundError:
            continue
        selections.append(selection)
        candidates_by_area[area.admin2_id] = candidates

    status_counts = {
        status: sum(selection.proposed_decision == status for selection in selections)
        for status in ("AUTO_ACCEPT", "REVIEW", "MISSING")
    }
    report: dict[str, Any] = {
        "schema_version": 1,
        "run_id": run_id,
        "country": config.iso3,
        "registry_count": len(areas),
        "searched_count": len(selections),
        "candidate_count": sum(len(value) for value in candidates_by_area.values()),
        "status_counts": status_counts,
        "search_pass_error_count": sum(len(selection.pass_errors) for selection in selections),
        "exa_cost_dollars": round(sum(_discovery_cost(config, run_id, area) for area in areas), 6),
    }
    acquisition_path = config.run_dir(run_id) / "reports" / "acquisition.json"
    if acquisition_path.is_file():
        acquisition = json.loads(acquisition_path.read_text(encoding="utf-8"))
        documents = acquisition.get("documents", []) if isinstance(acquisition, dict) else []
        report["uploaded_verified_count"] = sum(
            isinstance(item, dict) and item.get("status") == "UPLOADED_VERIFIED" for item in documents
        )
    else:
        report["uploaded_verified_count"] = 0

    if gold_path is not None:
        gold = _load_gold(gold_path)
        area_ids = {area.admin2_id for area in areas}
        unknown_area_ids = sorted(set(gold) - area_ids)
        if unknown_area_ids:
            raise ValueError(
                "Gold CSV contains unknown admin2_id values: " + ", ".join(unknown_area_ids)
            )
        selections_by_area = {selection.area.admin2_id: selection for selection in selections}
        ndcg_values: list[float] = []
        recall_hits = 0
        precision_hits = 0
        evaluated_areas = 0
        auto_hits = 0
        auto_total = 0
        field_matches = {"document_status": [], "start_year": [], "end_year": []}
        for admin2_id, gold_rows in gold.items():
            relevance_by_url = {
                str(row["candidate_url"]): int(row["relevance"]) for row in gold_rows
            }
            predicted = candidates_by_area.get(admin2_id, [])
            relevance = [
                relevance_by_url.get(canonicalize_url(item.identity_url), 0)
                for item in predicted
            ]
            ideal = sorted((int(row["relevance"]) for row in gold_rows), reverse=True)
            ideal_dcg = _dcg(ideal, 5)
            if ideal_dcg > 0:
                ndcg_values.append(_dcg(relevance, 5) / ideal_dcg)
            selection = selections_by_area.get(admin2_id)
            if selection and selection.proposed_decision == "AUTO_ACCEPT":
                auto_total += 1
                auto_hits += int(bool(relevance) and relevance[0] == 3)
            has_latest = any(int(row["relevance"]) == 3 for row in gold_rows)
            if not has_latest:
                continue
            evaluated_areas += 1
            recall_hits += int(3 in relevance[:10])
            precision_hits += int(bool(relevance) and relevance[0] == 3)
            if selection and selection.selected:
                selected_url = canonicalize_url(selection.selected.identity_url)
                selected_gold = next(
                    (row for row in gold_rows if row["candidate_url"] == selected_url),
                    None,
                )
                if selected_gold:
                    for field in field_matches:
                        expected = str(selected_gold.get(field) or "").strip()
                        if expected:
                            actual = str(getattr(selection.selected, field) or "")
                            field_matches[field].append(actual == expected)
        report["gold"] = {
            "path": str(gold_path.resolve()),
            "labeled_area_count": len(gold),
            "evaluated_area_count": evaluated_areas,
            "ndcg_at_5": round(sum(ndcg_values) / len(ndcg_values), 6) if ndcg_values else None,
            "recall_at_10": round(recall_hits / evaluated_areas, 6) if evaluated_areas else None,
            "latest_approved_precision_at_1": (
                round(precision_hits / evaluated_areas, 6) if evaluated_areas else None
            ),
            "auto_accept_precision": round(auto_hits / auto_total, 6) if auto_total else None,
            "auto_accept_count": auto_total,
            "field_exact_match": {
                field: round(sum(values) / len(values), 6) if values else None
                for field, values in field_matches.items()
            },
        }
    destination = config.run_dir(run_id) / "reports" / "evaluation.json"
    atomic_write_json(destination, report)
    return report
