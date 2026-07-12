from __future__ import annotations

import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


@contextmanager
def atomic_output_path(destination: Path) -> Iterator[Path]:
    """Yield a same-format temporary path and promote it only on success."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(
        f"{destination.stem}.part-{uuid.uuid4().hex}{destination.suffix}"
    )
    try:
        yield temporary
        if not temporary.is_file() or temporary.stat().st_size == 0:
            raise RuntimeError(f"Expected non-empty temporary output was not created: {temporary}")
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)


def validate_categorical_raster(
    path: Path,
    *,
    valid_classes: set[int],
    expected_nodata: int,
    max_sample_blocks: int = 256,
) -> dict[str, object]:
    """Validate categorical raster metadata and representative data blocks.

    Sampling bounds validation cost for national high-resolution rasters while
    still catching empty exports, nodata/class collisions, and unexpected class
    encodings. Processing performs the definitive full read later.
    """
    import numpy as np
    import rasterio

    with rasterio.open(path) as source:
        if source.count != 1:
            raise ValueError(f"Categorical raster must have one band: {path}")
        if source.crs is None or source.width <= 0 or source.height <= 0:
            raise ValueError(f"Categorical raster has invalid spatial metadata: {path}")
        if source.nodata is None or int(source.nodata) != expected_nodata:
            raise ValueError(
                f"Categorical raster {path} must use nodata={expected_nodata}, got {source.nodata}"
            )
        if expected_nodata in valid_classes:
            raise ValueError("Categorical nodata must not collide with a valid class")

        windows = [window for _, window in source.block_windows(1)]
        if len(windows) > max_sample_blocks:
            indices = np.linspace(0, len(windows) - 1, max_sample_blocks, dtype=int)
            windows = [windows[index] for index in indices]
        observed: set[int] = set()
        valid_pixels = 0
        for window in windows:
            values = source.read(1, window=window)
            unique, counts = np.unique(values, return_counts=True)
            for value, count in zip(unique.tolist(), counts.tolist(), strict=True):
                integer = int(value)
                if integer != expected_nodata:
                    observed.add(integer)
                    valid_pixels += int(count)
        unexpected = observed - valid_classes
        if unexpected:
            raise ValueError(f"Unexpected categorical classes in {path}: {sorted(unexpected)}")
        if valid_pixels == 0:
            raise ValueError(f"Categorical raster sample contains no valid pixels: {path}")
        return {
            "width": source.width,
            "height": source.height,
            "crs": str(source.crs),
            "nodata": source.nodata,
            "observed_classes": sorted(observed),
            "sample_valid_pixels": valid_pixels,
        }


def validate_numeric_raster(path: Path) -> dict[str, object]:
    """Perform a bounded validity check for a continuous raster export."""
    import numpy as np
    import rasterio

    with rasterio.open(path) as source:
        if source.count != 1 or source.crs is None:
            raise ValueError(f"Continuous raster has invalid metadata: {path}")
        height = min(source.height, 512)
        width = min(source.width, 512)
        values = source.read(
            1,
            out_shape=(height, width),
            masked=True,
        )
        finite = np.isfinite(values.compressed())
        if not finite.any():
            raise ValueError(f"Continuous raster sample contains no finite pixels: {path}")
        return {
            "width": source.width,
            "height": source.height,
            "crs": str(source.crs),
            "nodata": source.nodata,
        }
