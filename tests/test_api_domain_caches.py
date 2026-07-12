from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

import geopandas as gpd
import pandas as pd
import pytest
from shapely.geometry import Point, box, mapping

from ldt_factory.config import FactoryConfig
from ldt_factory.context import RunContext
from ldt_factory.domains._api_cache import (
    atomic_write_json,
    build_accessibility_entries,
)
from ldt_factory.domains.extract import accessibility as accessibility_extract
from ldt_factory.domains.extract import air_pollution as air_extract
from ldt_factory.domains.process import accessibility as accessibility_process
from ldt_factory.domains.process import air_pollution as air_process


def _logger() -> logging.Logger:
    logger = logging.getLogger("test-api-domain-caches")
    logger.handlers.clear()
    logger.addHandler(logging.NullHandler())
    return logger


def _context(tmp_path: Path, *, indicators: list[int] | None = None) -> RunContext:
    data = {
        "country": {"iso3": "TST", "name": "Testland"},
        "workspace": str(tmp_path),
        "boundaries": {
            "admin0": str(tmp_path / "admin0.geojson"),
            "admin1": str(tmp_path / "admin1.geojson"),
            "admin2": str(tmp_path / "admin2.geojson"),
            "admin1_source_field": "a1",
            "admin2_source_field": "a2",
            "admin1_output_name": "Admin1",
            "admin2_output_name": "Admin2",
        },
        "years": {
            "static_merge": 2025,
            "indicators": indicators or [2021],
        },
        "sources": {
            "openweathermap": {
                "api_key_env": "TEST_OWM_KEY",
                "grid_degrees": 1.0,
                "requests_per_minute": 60000,
            },
            "mapbox": {
                "access_token_env": "TEST_MAPBOX_TOKEN",
                "walking_distance_meters": 10000,
            },
        },
        "network": {"use_environment_proxy": False},
    }
    config = FactoryConfig(tmp_path / "test.yaml", data)
    config.prepare_directories()
    return RunContext(config, "test-run")


def _timestamp(year: int, month: int = 1) -> int:
    return int(datetime(year, month, 1, tzinfo=timezone.utc).timestamp())


def test_air_payload_summary_keeps_sufficient_statistics() -> None:
    payload = {
        "list": [
            {"dt": _timestamp(2021), "components": {"pm2_5": 10, "pm10": 20, "no2": 5}},
            {"dt": _timestamp(2021, 2), "components": {"pm2_5": 20, "pm10": 40, "no2": None}},
            {"dt": _timestamp(2022), "components": {"pm2_5": 30, "pm10": 60, "no2": 15}},
        ]
    }

    summary = air_extract.summarize_payload(
        payload,
        grid_id=7,
        requested_year=2021,
        lon=1.5,
        lat=2.5,
    )

    assert [row["year"] for row in summary["annual"]] == [2021, 2022]
    first = summary["annual"][0]["pollutants"]
    assert first["pm25"] == {"sum": 30.0, "count": 2, "mean": 15.0}
    assert first["no2"] == {"sum": 5.0, "count": 1, "mean": 5.0}


def test_air_processing_streams_compact_and_legacy_caches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = _context(tmp_path)
    grid = gpd.GeoDataFrame(
        {"grid_id": [0, 1]},
        geometry=[box(0, 0, 1, 1), box(1, 0, 2, 1)],
        crs="EPSG:4326",
    )
    grid.to_file(ctx.raw("air_pollution", "grid.geojson"), driver="GeoJSON", index=False)
    compact = air_extract.summarize_payload(
        {
            "list": [
                {"dt": _timestamp(2021), "components": {"pm2_5": 10, "pm10": 20, "no2": 4}},
                {"dt": _timestamp(2021, 2), "components": {"pm2_5": 20, "pm10": 40, "no2": 6}},
            ]
        },
        grid_id=0,
        requested_year=2021,
        lon=0.5,
        lat=0.5,
    )
    atomic_write_json(ctx.raw("air_pollution", "annual", "2021", "0.json"), compact)
    # The matching legacy response must not be counted a second time.
    atomic_write_json(
        ctx.raw("air_pollution", "0_2021.json"),
        {
            "factory_grid_id": 0,
            "list": [
                {"dt": _timestamp(2021), "components": {"pm2_5": 999, "pm10": 999, "no2": 999}}
            ],
        },
    )
    atomic_write_json(
        ctx.raw("air_pollution", "1_2021.json"),
        {
            "factory_grid_id": 1,
            "list": [
                {"dt": _timestamp(2021), "components": {"pm2_5": 30, "pm10": 50, "no2": 10}}
            ],
        },
    )
    admin = gpd.GeoDataFrame(
        {"Admin1": ["A"], "Admin2": ["B"]},
        geometry=[box(0, 0, 2, 1)],
        crs="EPSG:4326",
    )
    monkeypatch.setattr(air_process, "load_admin2", lambda config: admin)

    air_process.run(ctx, _logger())
    output = pd.read_csv(ctx.output("TST_air_pollution.csv"))
    # Preserve the notebook's observation-weighted mean rather than averaging
    # already-averaged grid cells with unequal observation counts.
    assert output.loc[0, "pm25"] == pytest.approx(20.0)
    assert output.loc[0, "pm10"] == pytest.approx(110.0 / 3.0)
    assert output.loc[0, "no2"] == pytest.approx(20.0 / 3.0)

    # A second run must reuse the cached grid-to-admin assignment.
    monkeypatch.setattr(gpd, "sjoin", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("sjoin called")))
    air_process.run(ctx, _logger())


def test_air_extraction_resumes_complete_cache_without_api_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = _context(tmp_path)
    admin0 = gpd.GeoDataFrame({"name": ["country"]}, geometry=[box(0, 0, 1, 1)], crs="EPSG:4326")
    monkeypatch.setattr(air_extract, "load_admin0", lambda config: admin0)
    monkeypatch.delenv("TEST_OWM_KEY", raising=False)
    atomic_write_json(
        ctx.raw("air_pollution", "annual", "2021", "0.json"),
        air_extract.summarize_payload(
            {"list": []},
            grid_id=0,
            requested_year=2021,
            lon=0.5,
            lat=0.5,
        ),
    )

    air_extract.run(ctx, _logger())

    manifest = json.loads(
        ctx.raw("air_pollution", "annual", "2021", "manifest.json").read_text(encoding="utf-8")
    )
    assert manifest["status"] == "complete"
    assert manifest["completed_cells"] == 1


def test_accessibility_cached_extraction_and_processing_are_offline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import numpy as np
    import rasterio
    from rasterio.transform import from_origin

    ctx = _context(tmp_path)
    assets = gpd.GeoDataFrame(
        {"osm_id": [1, 2, 3], "amenity": ["school", "school", "hospital"]},
        geometry=[Point(1, 1), Point(1, 1), Point(8, 8)],
        crs="EPSG:4326",
    )
    assets.to_parquet(ctx.config.shape_dir / "assets.parquet", index=False)
    entries = build_accessibility_entries(assets, distance_meters=10000, profile="walking")
    assert len(entries) == 2

    for entry in entries:
        geometry = box(0, 0, 10, 10) if entry["category"] == "hospital" else box(0, 0, 5, 10)
        atomic_write_json(
            ctx.raw("accessibility", "isochrones", f"{entry['cache_key']}.json"),
            {
                "schema_version": 1,
                "status": "complete",
                **entry,
                "profile": "walking",
                "distance_meters": 10000,
                "geometry": mapping(geometry),
            },
        )

    monkeypatch.delenv("TEST_MAPBOX_TOKEN", raising=False)
    accessibility_extract.run(ctx, _logger())
    manifest = json.loads(ctx.raw("accessibility", "manifest.json").read_text(encoding="utf-8"))
    assert manifest["status"] == "complete"
    assert manifest["completed_requests"] == 2

    population = ctx.raw("population", "tst_pop_2025_CN_100m_R2025A_v1.tif")
    with rasterio.open(
        population,
        "w",
        driver="GTiff",
        width=10,
        height=10,
        count=1,
        dtype="float32",
        crs="EPSG:4326",
        transform=from_origin(0, 10, 1, 1),
        nodata=0,
    ) as dataset:
        dataset.write(np.ones((10, 10), dtype="float32"), 1)
    admin = gpd.GeoDataFrame(
        {"Admin1": ["A"], "Admin2": ["B"]},
        geometry=[box(0, 0, 10, 10)],
        crs="EPSG:4326",
    )
    monkeypatch.setattr(accessibility_process, "load_admin2", lambda config: admin)

    accessibility_process.run(ctx, _logger())

    output = pd.read_csv(ctx.output("TST_accessibility.csv"))
    assert output.loc[0, "school_accessibility"] == pytest.approx(50.0)
    assert output.loc[0, "hospital_accessibility"] == pytest.approx(100.0)
