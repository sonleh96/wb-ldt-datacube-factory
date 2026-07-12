import logging
import warnings

import numpy as np
import pytest

gpd = pytest.importorskip("geopandas")
pytest.importorskip("pyarrow")
rasterstats = pytest.importorskip("rasterstats")
from rasterio.transform import from_origin
from shapely.geometry import LineString

from ldt_factory.domains.process.flood import _single_cell_depths, _summarize_line_risk


def test_single_cell_fast_path_uses_raster_nodata():
    geometries = gpd.GeoSeries(
        [
            LineString([(0.1, 0.5), (0.9, 0.5)]),
            LineString([(1.1, 0.5), (1.9, 0.5)]),
            LineString([(0.5, 0.5), (1.5, 0.5)]),
        ],
        crs="EPSG:4326",
    )
    positions, values = _single_cell_depths(
        geometries,
        np.array([[0.5, -999.0]], dtype="float32"),
        from_origin(0, 1, 1, 1),
        -999.0,
    )

    assert positions.tolist() == [0, 1]
    assert values[0] == pytest.approx(0.5)
    assert np.isnan(values[1])


def test_flood_batches_reuse_lengths_and_resume(tmp_path, monkeypatch):
    lines_path = tmp_path / "roads.parquet"
    raster_path = tmp_path / "flood.tif"
    raster_path.write_bytes(b"fingerprint-only")
    lines = gpd.GeoDataFrame(
        {
            "County": ["A", "A", "A"],
            "Municipality": ["West", "Middle", "East"],
            "length_km": [1.25, 7.5, 2.75],
        },
        geometry=[
            LineString([(0.1, 0.5), (0.9, 0.5)]),
            LineString([(1.1, 0.5), (1.9, 0.5)]),
            LineString([(2.1, 0.5), (2.9, 0.5)]),
        ],
        crs="EPSG:4326",
    )
    lines.to_parquet(lines_path, index=False)

    arguments = {
        "lines_path": lines_path,
        "raster_path": raster_path,
        "raster_array": np.array([[0.2, -999.0, 0.5]], dtype="float32"),
        "affine": from_origin(0, 1, 1, 1),
        "nodata": -999.0,
        "admin1_column": "County",
        "admin2_column": "Municipality",
        "output_name": "road_length_flood_risk",
        "label": "roads",
        "threshold": 0.3,
        "batch_size": 1,
        "state_dir": tmp_path / "state",
        "resume": True,
        "logger": logging.getLogger("test-flood"),
    }
    first = _summarize_line_risk(**arguments)

    assert first[["County", "Municipality"]].to_dict("records") == [
        {"County": "A", "Municipality": "East"}
    ]
    assert first.loc[0, "road_length_flood_risk"] == pytest.approx(2.75)
    assert len(list((tmp_path / "state").rglob("batch-*.parquet"))) == 3

    def fail_if_recomputed(*args, **kwargs):
        raise AssertionError("completed flood batches should be reused")

    monkeypatch.setattr(rasterstats, "zonal_stats", fail_if_recomputed)
    second = _summarize_line_risk(**arguments)
    assert second.equals(first)


def test_exact_fallback_handles_none_mean_without_pandas_dtype_warning(tmp_path):
    lines_path = tmp_path / "crossing.parquet"
    raster_path = tmp_path / "flood.tif"
    raster_path.write_bytes(b"fingerprint-only")
    gpd.GeoDataFrame(
        {"County": ["A"], "Municipality": ["B"], "length_km": [1.0]},
        geometry=[LineString([(0.5, 0.5), (1.5, 0.5)])],
        crs="EPSG:4326",
    ).to_parquet(lines_path, index=False)

    with warnings.catch_warnings():
        warnings.simplefilter("error", FutureWarning)
        output = _summarize_line_risk(
            lines_path=lines_path,
            raster_path=raster_path,
            raster_array=np.array([[-999.0, -999.0]], dtype="float32"),
            affine=from_origin(0, 1, 1, 1),
            nodata=-999.0,
            admin1_column="County",
            admin2_column="Municipality",
            output_name="road_length_flood_risk",
            label="roads",
            threshold=0.3,
            batch_size=10,
            state_dir=tmp_path / "state",
            resume=False,
            logger=logging.getLogger("test-flood-none"),
        )

    assert output.empty
