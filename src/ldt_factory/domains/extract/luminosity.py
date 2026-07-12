from __future__ import annotations

import logging

from ...context import RunContext
from ...logging_utils import logged_action
from ...raster_utils import atomic_output_path, validate_numeric_raster
from .earth_engine import configured_region, initialize


def run(ctx: RunContext, logger: logging.Logger) -> None:
    ee = initialize(ctx)
    region = configured_region(ctx, ee)
    download_threads = int(ctx.config.source("earth_engine").get("download_threads", 4))
    import geemap

    for year in ctx.config.years("indicators"):
        destination = ctx.raw("luminosity", f"{ctx.config.iso3}_{year}_annual.tif")
        if destination.is_file():
            try:
                metadata = validate_numeric_raster(destination)
                logger.info(
                    "reusing annual luminosity raster year=%d dimensions=%sx%s",
                    year,
                    metadata["width"],
                    metadata["height"],
                    extra={"action": "reuse", "domain": "luminosity", "phase": str(year), "path": str(destination)},
                )
                continue
            except ValueError as exc:
                logger.warning(
                    "existing annual luminosity raster is invalid and will be replaced year=%d error=%s",
                    year,
                    exc,
                    extra={"action": "validate", "domain": "luminosity", "phase": str(year), "path": str(destination)},
                )

        with logged_action(logger, "extract_annual", domain="luminosity", phase=str(year), path=str(destination)):
            collection = (
                ee.ImageCollection("NOAA/VIIRS/DNB/MONTHLY_V1/VCMSLCFG")
                .filterDate(f"{year}-01-01", f"{year + 1}-01-01")
                .select("avg_rad")
            )
            image = collection.map(lambda monthly: monthly.unmask(0)).sum().clip(region)
            with atomic_output_path(destination) as temporary:
                geemap.download_ee_image(
                    image=image,
                    filename=str(temporary),
                    region=region,
                    scale=500,
                    crs="EPSG:4326",
                    dtype="float32",
                    num_threads=download_threads,
                    max_tile_size=32,
                    max_tile_dim=10000,
                    unmask_value=-9999,
                    overwrite=True,
                )
                metadata = validate_numeric_raster(temporary)
            logger.info(
                "validated annual luminosity raster year=%d dimensions=%sx%s",
                year,
                metadata["width"],
                metadata["height"],
                extra={"action": "validate", "domain": "luminosity", "phase": str(year), "path": str(destination)},
            )
