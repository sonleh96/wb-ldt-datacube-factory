from __future__ import annotations

import json
import logging
import socket
import threading
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
import pytest
import rasterio
from rasterio.transform import from_origin
from shapely.geometry import box

from ldt_factory.checkpoint_utils import (
    checkpoint_matches,
    write_checkpoint_manifest,
    write_frame_parquet_atomic,
)
from ldt_factory.domains.process.emissions import _aggregate_sources
from ldt_factory.geo import read_osm_filtered
from ldt_factory.io_utils import download_file, extract_zip
from ldt_factory.prerequisites import _population_block_sums, _population_label_grid
from ldt_factory.raster_utils import validate_categorical_raster


def _logger() -> logging.Logger:
    logger = logging.getLogger("test-optimization-helpers")
    logger.handlers.clear()
    logger.addHandler(logging.NullHandler())
    return logger


def test_checkpoint_fingerprint_rejects_changed_inputs(tmp_path: Path) -> None:
    output = tmp_path / "result.parquet"
    manifest = tmp_path / "result.manifest.json"
    frame = pd.DataFrame({"value": [1]})
    expected = {"algorithm": "test-v1", "source_size": 10}
    write_frame_parquet_atomic(frame, output)
    write_checkpoint_manifest(manifest, expected, rows=1)

    assert checkpoint_matches(output, manifest, expected)
    assert not checkpoint_matches(output, manifest, {**expected, "source_size": 11})


def test_categorical_validation_rejects_nodata_class_collision(tmp_path: Path) -> None:
    path = tmp_path / "classes.tif"
    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        width=2,
        height=2,
        count=1,
        dtype="uint8",
        crs="EPSG:4326",
        transform=from_origin(0, 2, 1, 1),
        nodata=0,
    ) as dataset:
        dataset.write(np.array([[0, 1], [2, 3]], dtype="uint8"), 1)

    with pytest.raises(ValueError, match="nodata=255"):
        validate_categorical_raster(
            path,
            valid_classes=set(range(9)),
            expected_nodata=255,
        )


def test_osm_attribute_filter_is_case_insensitive_and_geometry_filtered(tmp_path: Path) -> None:
    path = tmp_path / "features.shp"
    source = gpd.GeoDataFrame(
        {"kind": ["school", "HOSPITAL", "shop"]},
        geometry=[box(0, 0, 1, 1), box(1, 0, 2, 1), box(2, 0, 3, 1)],
        crs="EPSG:4326",
    )
    source.to_file(path, index=False)

    selected = read_osm_filtered(
        path,
        category_column="kind",
        categories={"school", "hospital"},
        columns=["kind"],
    )

    assert selected["kind"].tolist() == ["school", "HOSPITAL"]
    assert len(selected.geometry) == 2


def test_zip_extraction_is_validated_and_reused(tmp_path: Path) -> None:
    archive = tmp_path / "source.zip"
    destination = tmp_path / "extracted"
    with zipfile.ZipFile(archive, "w") as package:
        package.writestr("folder/value.txt", "first")

    extract_zip(archive, destination)
    marker = json.loads((destination / ".ldt-extract.json").read_text(encoding="utf-8"))
    first_mtime = (destination / "folder" / "value.txt").stat().st_mtime_ns
    extract_zip(archive, destination)

    assert marker["size"] == archive.stat().st_size
    assert (destination / "folder" / "value.txt").read_text(encoding="utf-8") == "first"
    assert (destination / "folder" / "value.txt").stat().st_mtime_ns == first_mtime


def test_download_resumes_retained_partial_with_http_range(tmp_path: Path) -> None:
    content = (b"0123456789abcdef" * 200_000)[:3_000_000]

    class Handler(BaseHTTPRequestHandler):
        requests_seen = 0
        ranges: list[str | None] = []

        def log_message(self, *args):
            return

        def do_GET(self):  # noqa: N802 - stdlib handler API
            type(self).requests_seen += 1
            range_header = self.headers.get("Range")
            type(self).ranges.append(range_header)
            start = int(range_header.removeprefix("bytes=").split("-", 1)[0]) if range_header else 0
            if range_header:
                self.send_response(206)
                self.send_header("Content-Range", f"bytes {start}-{len(content) - 1}/{len(content)}")
            else:
                self.send_response(200)
            self.send_header("Content-Length", str(len(content) - start))
            self.send_header("ETag", '"test-etag"')
            self.end_headers()
            if type(self).requests_seen == 1:
                self.wfile.write(content[:1_500_000])
                self.wfile.flush()
                self.connection.shutdown(socket.SHUT_RDWR)
                self.connection.close()
                return
            self.wfile.write(content[start:])

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        destination = tmp_path / "download.bin"
        download_file(
            f"http://127.0.0.1:{server.server_port}/download.bin",
            destination,
            retries=3,
            timeout=(2, 5),
            use_environment_proxy=False,
        )
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()

    assert destination.read_bytes() == content
    assert Handler.requests_seen == 2
    assert Handler.ranges[0] is None
    assert Handler.ranges[1] is not None and Handler.ranges[1].startswith("bytes=")


def test_emissions_streaming_aggregates_chunks_and_filters_years(tmp_path: Path) -> None:
    source = tmp_path / "emissions.csv"
    pd.DataFrame(
        {
            "source_id": ["a", "b", "c"],
            "start_time": ["2021-01-01", "2021-06-01", "2026-01-01"],
            "lat": [0.5, 0.5, 0.5],
            "lon": [0.5, 1.5, 0.5],
            "emissions_quantity": [10.0, 20.0, 999.0],
        }
    ).to_csv(source, index=False)
    admin = gpd.GeoDataFrame(
        {"Admin1": ["A"], "Admin2": ["B"]},
        geometry=[box(0, 0, 2, 1)],
        crs="EPSG:4326",
    )

    output = _aggregate_sources(
        [source],
        admin,
        admin_columns=["Admin1", "Admin2"],
        allowed_years={2021},
        chunksize=1,
        logger=_logger(),
        gas="CO2",
    )

    assert output[["Admin1", "Admin2", "year"]].to_dict("records") == [
        {"Admin1": "A", "Admin2": "B", "year": 2021}
    ]
    assert output.loc[0, "emissions_quantity"] == pytest.approx(30.0)


def test_population_block_aggregation_matches_pixel_center_zonal_sum(tmp_path: Path) -> None:
    from rasterstats import zonal_stats

    raster_path = tmp_path / "population.tif"
    values = np.array([[1, 2, 3, 4], [5, -999, 7, 8]], dtype="float32")
    with rasterio.open(
        raster_path,
        "w",
        driver="GTiff",
        width=4,
        height=2,
        count=1,
        dtype="float32",
        crs="EPSG:4326",
        transform=from_origin(0, 2, 1, 1),
        nodata=-999,
        tiled=True,
        blockxsize=16,
        blockysize=16,
    ) as dataset:
        dataset.write(values, 1)
    admin = gpd.GeoDataFrame(
        {"Admin1": ["A", "A"], "Admin2": ["West", "East"]},
        geometry=[box(0, 0, 2, 2), box(2, 0, 4, 2)],
        crs="EPSG:4326",
    )

    labels, reference = _population_label_grid(admin, raster_path)
    blockwise = _population_block_sums(
        raster_path,
        labels,
        reference,
        region_count=2,
    )
    expected = [
        item.get("sum")
        for item in zonal_stats(admin.geometry, raster_path, stats=["sum"])
    ]

    assert blockwise.tolist() == pytest.approx(expected)
