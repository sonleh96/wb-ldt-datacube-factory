from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


class ConfigError(ValueError):
    """Raised when a country configuration is incomplete or inconsistent."""


@dataclass(frozen=True)
class FactoryConfig:
    path: Path
    data: dict[str, Any]

    @property
    def iso3(self) -> str:
        return str(self.data["country"]["iso3"]).upper()

    @property
    def country_name(self) -> str:
        return str(self.data["country"]["name"])

    @property
    def workspace(self) -> Path:
        return Path(self.data["workspace"]).expanduser().resolve()

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
        return dict(self.data.get("sources", {}).get(name, {}))

    def years(self, name: str) -> list[int]:
        value = self.data.get("years", {}).get(name, [])
        return [int(item) for item in value]

    def boundary_path(self, level: str) -> Path:
        return Path(self.data["boundaries"][level]).expanduser().resolve()

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


def load_config(path: str | Path, *, require_boundaries: bool = True) -> FactoryConfig:
    config_path = Path(path).expanduser().resolve()
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

    config = FactoryConfig(config_path, raw)
    if len(config.iso3) != 3:
        raise ConfigError(f"country.iso3 must be an ISO-3 code, got {config.iso3!r}")
    if config.admin1 == config.admin2:
        raise ConfigError("admin1_output_name and admin2_output_name must differ")
    if not isinstance(raw.get("pipeline", {}), dict):
        raise ConfigError("pipeline must be a YAML mapping")

    sources = raw.get("sources", {})
    if not isinstance(sources, dict):
        raise ConfigError("sources must be a YAML mapping")
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
