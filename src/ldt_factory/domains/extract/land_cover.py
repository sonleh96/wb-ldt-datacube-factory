from __future__ import annotations

import logging

from ...context import RunContext
from ...logging_utils import logged_action
from ...raster_utils import atomic_output_path, validate_categorical_raster
from .earth_engine import configured_region, initialize


NODATA_CLASS = 255


def run(ctx: RunContext, logger: logging.Logger) -> None:
    ee = initialize(ctx)
    region = configured_region(ctx, ee)
    pixel_size_m = int(ctx.config.source("land_cover").get("pixel_size_m", 10))
    download_threads = int(ctx.config.source("earth_engine").get("download_threads", 4))
    import geemap

    for year in ctx.config.years("land_cover"):
        destination = ctx.raw("land_cover", f"{ctx.config.iso3}_{year}.tif")
        if destination.is_file():
            try:
                metadata = validate_categorical_raster(
                    destination,
                    valid_classes=set(range(9)),
                    expected_nodata=NODATA_CLASS,
                )
                logger.info(
                    "reusing validated land-cover raster year=%d dimensions=%sx%s",
                    year,
                    metadata["width"],
                    metadata["height"],
                    extra={"action": "reuse", "domain": "land_cover", "phase": str(year), "path": str(destination)},
                )
                continue
            except ValueError as exc:
                logger.warning(
                    "existing land-cover raster is invalid and will be replaced year=%d error=%s",
                    year,
                    exc,
                    extra={"action": "validate", "domain": "land_cover", "phase": str(year), "path": str(destination)},
                )

        with logged_action(logger, "extract", domain="land_cover", phase=str(year), path=str(destination)):
            image = (
                ee.ImageCollection("GOOGLE/DYNAMICWORLD/V1")
                .filterDate(f"{year}-01-01", f"{year + 1}-01-01")
                .select("label")
                .mode()
                .unmask(NODATA_CLASS)
                .toUint8()
                .clip(region)
            )
            with atomic_output_path(destination) as temporary:
                geemap.download_ee_image(
                    image=image,
                    filename=str(temporary),
                    region=region,
                    scale=pixel_size_m,
                    crs="EPSG:4326",
                    dtype="uint8",
                    num_threads=download_threads,
                    max_tile_size=32,
                    max_tile_dim=10000,
                    unmask_value=NODATA_CLASS,
                    overwrite=True,
                )
                import rasterio

                with rasterio.open(temporary, "r+") as raster:
                    raster.nodata = NODATA_CLASS
                metadata = validate_categorical_raster(
                    temporary,
                    valid_classes=set(range(9)),
                    expected_nodata=NODATA_CLASS,
                )
            logger.info(
                "validated land-cover raster year=%d dimensions=%sx%s classes=%s",
                year,
                metadata["width"],
                metadata["height"],
                metadata["observed_classes"],
                extra={"action": "validate", "domain": "land_cover", "phase": str(year), "path": str(destination)},
            )
