import pytest

gpd = pytest.importorskip("geopandas")
from shapely.geometry import LineString, box

from ldt_factory.transport_utils import add_length_km, assign_and_clip_lines


def test_assigns_contained_lines_and_clips_crossing_lines():
    admin2 = gpd.GeoDataFrame(
        {
            "County": ["A", "A"],
            "Municipality": ["West", "East"],
        },
        geometry=[box(0, 0, 1, 1), box(1, 0, 2, 1)],
        crs="EPSG:4326",
    )
    lines = gpd.GeoDataFrame(
        {"osm_id": [1, 2, 3], "fclass": ["primary"] * 3},
        geometry=[
            LineString([(0.1, 0.5), (0.9, 0.5)]),
            LineString([(0.5, 0.5), (1.5, 0.5)]),
            LineString([(3, 3), (4, 4)]),
        ],
        crs="EPSG:4326",
    )

    result = assign_and_clip_lines(
        lines,
        admin2,
        admin1_column="County",
        admin2_column="Municipality",
    )

    assert len(result) == 3
    assert result.groupby("Municipality").size().to_dict() == {"East": 1, "West": 2}
    assert sum(geometry.length for geometry in result.geometry) == pytest.approx(1.8)
    assert set(result["osm_id"]) == {1, 2}


def test_add_length_keeps_source_crs_and_geometry():
    lines = gpd.GeoDataFrame(
        {"osm_id": [1]},
        geometry=[LineString([(26.0, 44.0), (26.01, 44.0)])],
        crs="EPSG:4326",
    )

    output = add_length_km(lines, "EPSG:6933")

    assert output.crs == lines.crs
    assert output.geometry.iloc[0].equals(lines.geometry.iloc[0])
    assert output.loc[0, "length_km"] > 0
