from __future__ import annotations

import logging
from pathlib import Path

import geopandas as gpd
import pandas as pd
import pytest
from shapely.geometry import box

from ldt_factory.combine import run
from ldt_factory.config import FactoryConfig
from ldt_factory.context import RunContext


def _context(tmp_path: Path) -> RunContext:
    boundary_path = tmp_path / "admin2.geojson"
    gpd.GeoDataFrame(
        {"a1": ["A"], "a2": ["B"]},
        geometry=[box(0, 0, 1, 1)],
        crs="EPSG:4326",
    ).to_file(boundary_path, driver="GeoJSON", index=False)
    config = FactoryConfig(
        tmp_path / "test.yaml",
        {
            "country": {"iso3": "TST", "name": "Testland"},
            "workspace": str(tmp_path / "workspace"),
            "boundaries": {
                "admin0": str(boundary_path),
                "admin1": str(boundary_path),
                "admin2": str(boundary_path),
                "admin1_source_field": "a1",
                "admin2_source_field": "a2",
                "admin1_output_name": "Admin1",
                "admin2_output_name": "Admin2",
            },
            "years": {"indicators": [2021, 2022], "static_merge": 2022},
            "pipeline": {"main_domains": [], "strict_output_coverage": True},
            "scoring": {"rank_within_year": False},
        },
    )
    config.prepare_directories()
    return RunContext(config, "test")


def _logger() -> logging.Logger:
    logger = logging.getLogger("test-combine")
    logger.handlers.clear()
    logger.addHandler(logging.NullHandler())
    return logger


def _write_inputs(context: RunContext, population_years=(2021, 2022)) -> None:
    population_values = [100.0] * len(population_years)
    if population_years == (2021, 2022):
        population_values[-1] = None
    pd.DataFrame(
        {
            "Admin1": ["A"] * len(population_years),
            "Admin2": ["B"] * len(population_years),
            "year": list(population_years),
            "population_total": population_values,
        }
    ).to_csv(context.config.dataset_dir / "TST_population.csv", index=False)
    pd.DataFrame(
        {
            "Admin1": ["A"],
            "Admin2": ["B"],
            "year": [2022],
            "road_length": [10.0],
            "rail_length": [2.0],
            "transport_source_year": [2026],
            "flood_scenario_year": [2030],
            "flood_return_period_years": [100],
            "key_structures": [5],
        }
    ).to_csv(context.config.dataset_dir / "TST_infra_length.csv", index=False)
    pd.DataFrame(
        {
            "Admin1": ["A"],
            "Admin2": ["B"],
            "year": [2022],
            "hospital_accessibility": [75.0],
            "school_accessibility": [50.0],
        }
    ).to_csv(context.config.dataset_dir / "TST_accessibility.csv", index=False)


def test_combine_uses_configured_publication_domains_and_validates_coverage(tmp_path: Path):
    context = _context(tmp_path)
    _write_inputs(context)

    indicators_path, scores_path = run(context, _logger())

    indicators = pd.read_csv(indicators_path)
    scores = pd.read_csv(scores_path)
    assert len(indicators) == 2
    assert indicators["Population"].tolist() == [100.0, 0.0]
    assert indicators["Total Road Length (km)"].tolist() == [10.0, 10.0]
    assert indicators["Total Railway Length (km)"].tolist() == [2.0, 2.0]
    assert indicators["Accessibility to Hospitals (%)"].tolist() == [75.0, 75.0]
    assert indicators["Accessibility to Schools (%)"].tolist() == [50.0, 50.0]
    assert not indicators.isna().any().any()
    assert not scores.isna().any().any()
    for column in (
        "transport_source_year",
        "flood_scenario_year",
        "flood_return_period_years",
        "key_structures",
    ):
        assert column not in indicators
        assert column not in scores
    assert "Accessibility to Hospitals Score" in scores
    assert "Accessibility to Schools Score" in scores


def test_combine_rejects_keys_outside_configured_years(tmp_path: Path):
    context = _context(tmp_path)
    _write_inputs(context, population_years=(2021, 2022, 2026))

    with pytest.raises(ValueError, match="outside the configured"):
        run(context, _logger())


def test_combine_requires_accessibility_output(tmp_path: Path):
    context = _context(tmp_path)
    _write_inputs(context)
    (context.config.dataset_dir / "TST_accessibility.csv").unlink()

    with pytest.raises(FileNotFoundError, match="TST_accessibility.csv"):
        run(context, _logger())
