from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


class PlanConfigError(ValueError):
    """Raised when a development-plan configuration is invalid."""


def _required_mapping(data: dict[str, Any], name: str) -> dict[str, Any]:
    value = data.get(name)
    if not isinstance(value, dict):
        raise PlanConfigError(f"{name} must be a YAML mapping")
    return value


def _required_text(data: dict[str, Any], name: str) -> str:
    value = data.get(name)
    if not isinstance(value, str) or not value.strip():
        raise PlanConfigError(f"{name} must be a non-empty string")
    return value.strip()


def _text_list(data: dict[str, Any], name: str, *, required: bool = False) -> tuple[str, ...]:
    value = data.get(name, [])
    if not isinstance(value, list) or any(not isinstance(item, str) or not item.strip() for item in value):
        raise PlanConfigError(f"{name} must be a list of non-empty strings")
    if required and not value:
        raise PlanConfigError(f"{name} must contain at least one value")
    return tuple(item.strip() for item in value)


def _resolve_path(value: str, *, data_root: Path | None, field: str) -> Path:
    expanded = Path(os.path.expandvars(os.path.expanduser(value)))
    if expanded.is_absolute():
        return expanded
    if data_root is None:
        raise PlanConfigError(
            f"{field} is relative but no data root is configured; pass --data-root, "
            "set LDT_DATA_ROOT, or add data_root to the YAML"
        )
    return data_root / expanded


@dataclass(frozen=True)
class DevelopmentPlanConfig:
    path: Path
    raw: dict[str, Any]
    data_root: Path | None
    workspace: Path
    registry_path: Path

    @property
    def iso3(self) -> str:
        return str(self.raw["country"]["iso3"]).upper()

    @property
    def country_name(self) -> str:
        return str(self.raw["country"]["name"])

    @property
    def languages(self) -> tuple[str, ...]:
        return tuple(self.raw["country"]["languages"])

    @property
    def admin2_types(self) -> tuple[str, ...]:
        return tuple(self.raw["country"].get("admin2_types", ()))

    @property
    def registry(self) -> dict[str, Any]:
        return self.raw["admin_registry"]

    @property
    def search(self) -> dict[str, Any]:
        return self.raw["search"]

    @property
    def review(self) -> dict[str, Any]:
        return self.raw["review"]

    @property
    def storage(self) -> dict[str, Any]:
        return self.raw["storage"]

    @property
    def api_key_env(self) -> str:
        return str(self.search.get("api_key_env", "EXA_API_KEY"))

    @property
    def bucket(self) -> str:
        return str(self.storage["bucket"])

    @property
    def storage_prefix(self) -> str:
        return str(self.storage["prefix"]).strip("/")

    @property
    def runs_dir(self) -> Path:
        return self.workspace / "runs"

    def run_dir(self, run_id: str) -> Path:
        return self.runs_dir / run_id

    def prepare_run(self, run_id: str) -> Path:
        run_dir = self.run_dir(run_id)
        for name in ("candidates", "decisions", "downloads", "logs", "reports"):
            (run_dir / name).mkdir(parents=True, exist_ok=True)
        return run_dir


def load_plan_config(
    path: str | Path,
    *,
    data_root: str | Path | None = None,
    require_registry: bool = True,
) -> DevelopmentPlanConfig:
    config_path = Path(path)
    with config_path.open("r", encoding="utf-8-sig") as handle:
        raw = yaml.safe_load(handle)
    if not isinstance(raw, dict):
        raise PlanConfigError("development-plan configuration must be a YAML mapping")
    if raw.get("schema_version") != 1:
        raise PlanConfigError("schema_version must be 1")

    country = _required_mapping(raw, "country")
    iso3 = _required_text(country, "iso3").upper()
    if len(iso3) != 3 or not iso3.isalpha():
        raise PlanConfigError(f"country.iso3 must be an ISO-3 code, got {iso3!r}")
    _required_text(country, "name")
    country["languages"] = list(_text_list(country, "languages", required=True))
    country["admin2_types"] = list(_text_list(country, "admin2_types"))

    registry = _required_mapping(raw, "admin_registry")
    _required_text(registry, "path")
    _required_text(registry, "name_field")
    for field in ("id_field", "parent_field", "storage_slug_field"):
        value = registry.get(field)
        if value is not None and (not isinstance(value, str) or not value.strip()):
            raise PlanConfigError(f"admin_registry.{field} must be a non-empty string when set")
    registry["alias_fields"] = list(_text_list(registry, "alias_fields"))
    slug_mode = str(registry.get("slug_mode", "ascii_replace"))
    if slug_mode not in {"ascii_replace", "transliterate"}:
        raise PlanConfigError("admin_registry.slug_mode must be ascii_replace or transliterate")
    registry["slug_mode"] = slug_mode

    search = _required_mapping(raw, "search")
    search["document_terms"] = list(_text_list(search, "document_terms", required=True))
    search["final_terms"] = list(_text_list(search, "final_terms"))
    search["draft_terms"] = list(_text_list(search, "draft_terms"))
    search["official_domain_suffixes"] = list(_text_list(search, "official_domain_suffixes"))
    search["api_key_env"] = _required_text(search, "api_key_env")
    for field, default, minimum, maximum in (
        ("max_passes", 3, 1, 3),
        ("num_results", 10, 1, 100),
        ("request_timeout_seconds", 60, 1, 300),
    ):
        value = search.get(field, default)
        if not isinstance(value, int) or not minimum <= value <= maximum:
            raise PlanConfigError(f"search.{field} must be an integer from {minimum} to {maximum}")
        search[field] = value

    review = _required_mapping(raw, "review")
    for field, default in (("auto_accept_threshold", 0.9), ("minimum_margin", 0.1)):
        value = review.get(field, default)
        if not isinstance(value, (int, float)) or not 0 <= float(value) <= 1:
            raise PlanConfigError(f"review.{field} must be between 0 and 1")
        review[field] = float(value)
    review.setdefault("require_formal_adoption_for_auto_accept", True)

    storage = _required_mapping(raw, "storage")
    _required_text(storage, "bucket")
    prefix = _required_text(storage, "prefix").strip("/")
    if not prefix:
        raise PlanConfigError("storage.prefix cannot be the bucket root")
    storage["prefix"] = prefix

    root_value = data_root or os.environ.get("LDT_DATA_ROOT") or raw.get("data_root")
    resolved_root = Path(root_value).resolve() if root_value else None
    workspace = _resolve_path(
        _required_text(raw, "workspace"),
        data_root=resolved_root,
        field="workspace",
    )
    registry_path = _resolve_path(
        str(registry["path"]),
        data_root=resolved_root,
        field="admin_registry.path",
    )
    if require_registry and not registry_path.is_file():
        raise PlanConfigError(f"admin registry does not exist: {registry_path}")

    return DevelopmentPlanConfig(
        path=config_path.resolve(),
        raw=raw,
        data_root=resolved_root,
        workspace=workspace.resolve(),
        registry_path=registry_path.resolve(),
    )
