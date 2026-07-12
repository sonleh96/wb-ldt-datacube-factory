import numpy as np
import pandas as pd
import pytest
from pathlib import Path
import logging

pytest.importorskip("netCDF4")

from ldt_factory.config import FactoryConfig
from ldt_factory.context import RunContext
from ldt_factory.domains.extract import heatwaves as heatwave_extract
from ldt_factory.domains.process import heatwaves
from ldt_factory.domains.process.heatwaves import _event_count, _has_event


def test_event_count_matches_notebook_non_overlapping_behavior():
    threshold = 313.15
    values = np.array([314] * 12 + [300] + [314] * 5)
    assert _event_count(values, threshold, 5) == 3


def test_event_count_resets_below_threshold():
    threshold = 313.15
    values = np.array([314] * 4 + [300] + [314] * 4)
    assert _event_count(values, threshold, 5) == 0


def test_binary_event_detector_short_circuits_with_same_presence_result():
    threshold = 313.15
    values = np.array([314] * 100_000 + [300])
    assert _has_event(values, threshold, 5) == 1
    assert bool(_event_count(values, threshold, 5)) is True
    assert _has_event(np.array([314] * 4 + [300]), threshold, 5) == 0


def test_heatwave_processing_uses_dissolved_binary_mask_and_existing_lengths(tmp_path: Path):
    gpd = pytest.importorskip("geopandas")
    xr = pytest.importorskip("xarray")
    from shapely.geometry import LineString, box

    boundary_path = tmp_path / "admin2.geojson"
    boundary = gpd.GeoDataFrame(
        {"a1": ["A"], "a2": ["B"]},
        geometry=[box(0, 0, 2, 2)],
        crs="EPSG:4326",
    )
    boundary.to_file(boundary_path, driver="GeoJSON", index=False)
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
            "years": {"transport": 2026, "static_merge": 2025},
            "sources": {
                "heatwaves": {
                    "variable": "tasmax",
                    "threshold_c": 40,
                    "consecutive_days": 5,
                    "processing_spatial_chunk_cells": 2,
                    "dask_workers": 1,
                }
            },
            "processing": {"transport": {"metric_crs": "EPSG:6933"}},
        },
    )
    config.prepare_directories()
    context = RunContext(config, "test")
    heatwave_dir = context.raw("heatwaves")
    heatwave_dir.mkdir(parents=True, exist_ok=True)
    values = np.full((6, 2, 2), 300.0, dtype="float32")
    values[:5, 0, 0] = 314.0
    xr.Dataset(
        {"tasmax": (("time", "lat", "lon"), values)},
        coords={"time": np.arange(6), "lat": [0.5, 1.5], "lon": [0.5, 1.5]},
    ).to_netcdf(heatwave_dir / "projection_TST.nc")

    roads = gpd.GeoDataFrame(
        {"Admin1": ["A"], "Admin2": ["B"], "length_km": [1.25]},
        geometry=[LineString([(0.1, 0.5), (0.9, 0.5)])],
        crs="EPSG:4326",
    )
    rails = gpd.GeoDataFrame(
        {"Admin1": ["A"], "Admin2": ["B"], "length_km": [2.0]},
        geometry=[LineString([(1.1, 1.5), (1.9, 1.5)])],
        crs="EPSG:4326",
    )
    roads.to_parquet(config.shape_dir / "roads_intersect_2026.parquet", index=False)
    rails.to_parquet(config.shape_dir / "rails_intersect_2026.parquet", index=False)
    logger = logging.getLogger("test-heatwave-processing")
    logger.handlers.clear()
    logger.addHandler(logging.NullHandler())

    heatwaves.run(context, logger)

    output = pd.read_csv(config.dataset_dir / "TST_heatwaves.csv")
    assert output.loc[0, "road_length_heatwave_risk"] == pytest.approx(1.25)
    assert output.loc[0, "railway_length_heatwave_risk"] == pytest.approx(0.0)
    assert (config.workspace / "state" / "heatwaves" / "risk_mask.parquet").is_file()


def test_heatwave_extraction_subsets_and_writes_valid_compressed_clip(tmp_path: Path):
    gpd = pytest.importorskip("geopandas")
    xr = pytest.importorskip("xarray")
    from shapely.geometry import box

    boundary_path = tmp_path / "admin0.geojson"
    gpd.GeoDataFrame(
        {"name": ["country"]},
        geometry=[box(0, 0, 2, 2)],
        crs="EPSG:4326",
    ).to_file(boundary_path, driver="GeoJSON", index=False)
    source_path = tmp_path / "global.nc"
    xr.Dataset(
        {
            "tasmax": (
                ("time", "lat", "lon"),
                np.full((3, 5, 5), 314.0, dtype="float32"),
            )
        },
        coords={
            "time": np.arange(3),
            "lat": [-1.0, 0.0, 1.0, 2.0, 3.0],
            "lon": [-1.0, 0.0, 1.0, 2.0, 3.0],
        },
    ).to_netcdf(source_path)
    config = FactoryConfig(
        tmp_path / "test.yaml",
        {
            "country": {"iso3": "TST", "name": "Testland"},
            "workspace": str(tmp_path / "workspace"),
            "boundaries": {
                "admin0": str(boundary_path),
                "admin1": str(boundary_path),
                "admin2": str(boundary_path),
                "admin1_source_field": "name",
                "admin2_source_field": "name",
                "admin1_output_name": "Admin1",
                "admin2_output_name": "Admin2",
            },
            "sources": {
                "heatwaves": {
                    "source_glob": str(source_path),
                    "variable": "tasmax",
                    "spatial_chunk_cells": 2,
                    "extraction_time_chunk_days": 2,
                }
            },
        },
    )
    config.prepare_directories()
    context = RunContext(config, "test")
    logger = logging.getLogger("test-heatwave-extraction")
    logger.handlers.clear()
    logger.addHandler(logging.NullHandler())

    heatwave_extract.run(context, logger)

    output = context.raw("heatwaves", "global_TST.nc")
    with xr.open_dataset(output) as clipped:
        assert clipped["tasmax"].sizes["lat"] < 5
        assert clipped["tasmax"].sizes["lon"] < 5
        assert clipped["tasmax"].sizes["time"] == 3
