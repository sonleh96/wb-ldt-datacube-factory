from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


class ConfigError(ValueError):
    """Raised when a country configuration is incomplete or inconsistent."""


_ENVIRONMENT_VARIABLE = re.compile(
    r"\$(?:\{(?P<braced>[A-Za-z_][A-Za-z0-9_]*)\}|(?P<plain>[A-Za-z_][A-Za-z0-9_]*))"
)
_SOURCE_PATH_FIELDS = ("cache_dir", "dataset_root", "raw_dir", "source_glob")


def _expand_path_variables(
    value: str | Path,
    *,
    field: str,
    overrides: dict[str, str] | None = None,
) -> str:
    variables = dict(os.environ)
    if overrides:
        variables.update(overrides)
    missing: set[str] = set()

    def replace(match: re.Match[str]) -> str:
        name = match.group("braced") or match.group("plain")
        if name not in variables:
            missing.add(name)
            return match.group(0)
        return variables[name]

    expanded = _ENVIRONMENT_VARIABLE.sub(replace, str(value))
    if missing:
        names = ", ".join(sorted(missing))
        raise ConfigError(f"Unresolved environment variable(s) in {field}: {names}")
    return expanded


def _resolve_data_root(
    value: str | Path | None,
    *,
    config_path: Path,
    from_config: bool,
) -> Path | None:
    if value in (None, ""):
        return None
    expanded = _expand_path_variables(value, field="data_root")
    candidate = Path(expanded).expanduser()
    if not candidate.is_absolute():
        candidate = (config_path.parent if from_config else Path.cwd()) / candidate
    return candidate.resolve()


@dataclass(frozen=True)
class FactoryConfig:
    path: Path
    data: dict[str, Any]
    data_root: Path | None = None

    @property
    def iso3(self) -> str:
        return str(self.data["country"]["iso3"]).upper()

    @property
    def country_name(self) -> str:
        return str(self.data["country"]["name"])

    @property
    def workspace(self) -> Path:
        return self.resolve_path(self.data["workspace"], field="workspace")

    @property
    def raw_dir(self) -> Path:
        return self.workspace / "raw_data"

    @property
    def dataset_dir(self) -> Path:
        return self.workspace / "datasets"

    @property
    def shape_dir(self) -> Path:
        return self.workspace / "shapefiles"

    @property
    def admin1(self) -> str:
        return str(self.data["boundaries"]["admin1_output_name"])

    @property
    def admin2(self) -> str:
        return str(self.data["boundaries"]["admin2_output_name"])

    @property
    def pipeline(self) -> dict[str, Any]:
        value = self.data.get("pipeline", {})
        return dict(value) if isinstance(value, dict) else {}

    @property
    def resume_completed(self) -> bool:
        return bool(self.pipeline.get("resume_completed", True))

    def source(self, name: str) -> dict[str, Any]:
        source = dict(self.data.get("sources", {}).get(name, {}))
        for field in _SOURCE_PATH_FIELDS:
            if source.get(field) not in (None, ""):
                source[field] = str(
                    self.resolve_path(
                        source[field],
                        field=f"sources.{name}.{field}",
                    )
                )
        return source

    def years(self, name: str) -> list[int]:
        value = self.data.get("years", {}).get(name, [])
        return [int(item) for item in value]

    def boundary_path(self, level: str) -> Path:
        return self.resolve_path(
            self.data["boundaries"][level],
            field=f"boundaries.{level}",
            base=self.workspace,
        )

    def resolve_path(
        self,
        value: str | Path,
        *,
        field: str,
        base: Path | None = None,
    ) -> Path:
        overrides = {"LDT_DATA_ROOT": str(self.data_root)} if self.data_root else None
        expanded = _expand_path_variables(value, field=field, overrides=overrides)
        candidate = Path(expanded).expanduser()
        if not candidate.is_absolute():
            root = base or self.data_root
            if root is None:
                raise ConfigError(
                    f"Relative path in {field} requires --data-root, "
                    "LDT_DATA_ROOT, or data_root in the country YAML"
                )
            candidate = root / candidate
        return candidate.resolve()

    def prepare_directories(self) -> None:
        for path in (
            self.workspace,
            self.raw_dir,
            self.dataset_dir,
            self.shape_dir,
            self.workspace / "logs",
            self.workspace / "state",
        ):
            path.mkdir(parents=True, exist_ok=True)


def load_config(
    path: str | Path,
    *,
    require_boundaries: bool = True,
    data_root: str | Path | None = None,
) -> FactoryConfig:
    config_path = Path(
        _expand_path_variables(path, field="config")
    ).expanduser().resolve()
    if not config_path.is_file():
        raise ConfigError(f"Configuration file does not exist: {config_path}")
    with config_path.open("r", encoding="utf-8") as handle:
        raw = yaml.safe_load(handle)
    if not isinstance(raw, dict):
        raise ConfigError("Configuration root must be a YAML mapping")

    required = {
        "country.iso3": raw.get("country", {}).get("iso3"),
        "country.name": raw.get("country", {}).get("name"),
        "workspace": raw.get("workspace"),
        "boundaries.admin0": raw.get("boundaries", {}).get("admin0"),
        "boundaries.admin1": raw.get("boundaries", {}).get("admin1"),
        "boundaries.admin2": raw.get("boundaries", {}).get("admin2"),
        "boundaries.admin1_source_field": raw.get("boundaries", {}).get("admin1_source_field"),
        "boundaries.admin2_source_field": raw.get("boundaries", {}).get("admin2_source_field"),
        "boundaries.admin1_output_name": raw.get("boundaries", {}).get("admin1_output_name"),
        "boundaries.admin2_output_name": raw.get("boundaries", {}).get("admin2_output_name"),
    }
    missing = [key for key, value in required.items() if value in (None, "")]
    if missing:
        raise ConfigError(f"Missing required configuration values: {', '.join(missing)}")

    configured_root = data_root
    from_config = False
    if configured_root in (None, ""):
        configured_root = os.environ.get("LDT_DATA_ROOT")
    if configured_root in (None, ""):
        configured_root = raw.get("data_root")
        from_config = configured_root not in (None, "")
    resolved_root = _resolve_data_root(
        configured_root,
        config_path=config_path,
        from_config=from_config,
    )
    config = FactoryConfig(config_path, raw, resolved_root)
    # Resolve all configured path fields during validation so path errors fail
    # before a task starts or creates output directories.
    config.workspace
    for level in ("admin0", "admin1", "admin2"):
        config.boundary_path(level)
    if len(config.iso3) != 3:
        raise ConfigError(f"country.iso3 must be an ISO-3 code, got {config.iso3!r}")
    if config.admin1 == config.admin2:
        raise ConfigError("admin1_output_name and admin2_output_name must differ")
    if not isinstance(raw.get("pipeline", {}), dict):
        raise ConfigError("pipeline must be a YAML mapping")

    sources = raw.get("sources", {})
    if not isinstance(sources, dict):
        raise ConfigError("sources must be a YAML mapping")
    for source_name in sources:
        config.source(source_name)
    land_cover = sources.get("land_cover", {})
    earth_engine = sources.get("earth_engine", {})
    if not isinstance(land_cover, dict):
        raise ConfigError("sources.land_cover must be a YAML mapping")
    if not isinstance(earth_engine, dict):
        raise ConfigError("sources.earth_engine must be a YAML mapping")
    for source_name in ("heatwaves", "internet"):
        source = sources.get(source_name, {})
        if not isinstance(source, dict):
            raise ConfigError(f"sources.{source_name} must be a YAML mapping")
        provider = source.get("provider", "local")
        if provider not in {"local", "google_drive"}:
            raise ConfigError(
                f"sources.{source_name}.provider must be local or google_drive"
            )
        if provider == "google_drive":
            missing_drive = [
                f"sources.{source_name}.{field}"
                for field in ("drive_folder_id",)
                if source.get(field) in (None, "")
            ]
            if not source.get("cache_dir") and not (
                source_name == "internet" and source.get("dataset_root")
            ):
                missing_drive.append(f"sources.{source_name}.cache_dir")
            if missing_drive:
                raise ConfigError(
                    "Google Drive sources require: " + ", ".join(missing_drive)
                )
            for field, default in (("request_timeout_seconds", 120), ("download_retries", 5)):
                try:
                    value = int(source.get(field, default))
                except (TypeError, ValueError) as error:
                    raise ConfigError(f"sources.{source_name}.{field} must be an integer") from error
                if value < 1:
                    raise ConfigError(f"sources.{source_name}.{field} must be at least 1")
    internet = sources.get("internet", {})
    if isinstance(internet, dict):
        try:
            batch_size = int(internet.get("combine_batch_size", 131_072))
        except (TypeError, ValueError) as error:
            raise ConfigError("sources.internet.combine_batch_size must be an integer") from error
        if batch_size < 1:
            raise ConfigError("sources.internet.combine_batch_size must be at least 1")

    from .domains.land_cover_contract import (
        GEE_REDUCE_REGIONS_BACKEND,
        LAND_COVER_BACKENDS,
        land_cover_backend,
    )

    backend = land_cover_backend(config)
    if backend not in LAND_COVER_BACKENDS:
        raise ConfigError(
            "sources.land_cover.backend must be one of: "
            + ", ".join(sorted(LAND_COVER_BACKENDS))
        )
    numeric_options = {
        "pixel_size_m": (land_cover.get("pixel_size_m", 10), float),
        "tile_scale": (land_cover.get("tile_scale", 4), float),
        "max_pixels_per_region": (
            land_cover.get("max_pixels_per_region", 1_000_000_000),
            int,
        ),
        "poll_seconds": (land_cover.get("poll_seconds", 20), float),
    }
    for name, (value, converter) in numeric_options.items():
        try:
            converted = converter(value)
        except (TypeError, ValueError) as error:
            raise ConfigError(f"sources.land_cover.{name} must be numeric") from error
        if converted <= 0:
            raise ConfigError(f"sources.land_cover.{name} must be positive")
    cleanup = land_cover.get("cleanup_intermediate_assets", False)
    if not isinstance(cleanup, bool):
        raise ConfigError("sources.land_cover.cleanup_intermediate_assets must be boolean")

    if backend == GEE_REDUCE_REGIONS_BACKEND:
        missing_gee = [
            f"sources.earth_engine.{name}"
            for name in ("project_id", "admin2_asset_id")
            if earth_engine.get(name) in (None, "")
        ]
        if missing_gee:
            raise ConfigError(
                "The gee_reduce_regions Land Cover backend requires: "
                + ", ".join(missing_gee)
            )
        try:
            from .domains.land_cover_contract import intermediate_asset_prefix

            intermediate_asset_prefix(config)
        except ValueError as error:
            raise ConfigError(str(error)) from error
    # Import lazily to keep the configuration types usable by the scheduler
    # without introducing a module-level cycle.
    from .resources import ResourcePolicy

    ResourcePolicy.from_config(config)
    if require_boundaries:
        absent = [str(config.boundary_path(level)) for level in ("admin0", "admin1", "admin2") if not config.boundary_path(level).is_file()]
        if absent:
            raise ConfigError("Boundary files do not exist: " + ", ".join(absent))
    return config
