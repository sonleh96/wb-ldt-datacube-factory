import pandas as pd
import pytest

pytest.importorskip("pyarrow")

from ldt_factory.domains.process.internet import (
    _prefix_upper_bound,
    _quadkey_from_tile,
    _quadkey_prefixes_for_bounds,
    _read_fixed_candidates,
    _read_mobile_candidates,
    _tile_x,
    _tile_y,
)


def test_quadkey_bbox_prefixes_cover_every_bbox_corner():
    bounds = (20.26, 43.61, 29.72, 48.27)
    zoom = 8
    prefixes = _quadkey_prefixes_for_bounds(bounds, zoom)

    for longitude in (bounds[0], bounds[2]):
        for latitude in (bounds[1], bounds[3]):
            key = _quadkey_from_tile(
                _tile_x(longitude, zoom),
                _tile_y(latitude, zoom),
                zoom,
            )
            assert key in prefixes


def test_prefix_upper_bound_handles_carry():
    assert _prefix_upper_bound("1203") == "121"
    assert _prefix_upper_bound("333") is None


def test_arrow_filters_fixed_keys_and_mobile_prefixes(tmp_path):
    fixed_path = tmp_path / "fixed.parquet"
    pd.DataFrame(
        {
            "quadkey": ["1200000000000000", "1200000000000000", "1210000000000000"],
            "avg_d_kbps": [10.0, 20.0, 30.0],
            "unused": [1, 2, 3],
        }
    ).to_parquet(fixed_path, index=False)
    fixed = _read_fixed_candidates(fixed_path, ["1200000000000000"])
    assert fixed["avg_d_kbps"].tolist() == [10.0, 20.0]
    assert fixed.columns.tolist() == ["quadkey", "avg_d_kbps"]

    mobile_path = tmp_path / "mobile.parquet"
    pd.DataFrame(
        {
            "quadkey": ["1200000000000000", "1203333333333333", "1210000000000000"],
            "avg_d_kbps": [10.0, 20.0, 30.0],
            "tile": [
                "POLYGON ((0 0, 0 1, 1 1, 1 0, 0 0))",
                "POLYGON ((1 0, 1 1, 2 1, 2 0, 1 0))",
                "POLYGON ((2 0, 2 1, 3 1, 3 0, 2 0))",
            ],
        }
    ).to_parquet(mobile_path, index=False)
    mobile = _read_mobile_candidates(mobile_path, ["120"])
    assert mobile["quadkey"].tolist() == ["1200000000000000", "1203333333333333"]
