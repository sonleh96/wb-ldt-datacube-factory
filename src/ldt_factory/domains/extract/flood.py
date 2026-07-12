from __future__ import annotations

import logging

from ...context import RunContext
from ...logging_utils import logged_action
from ...raster_utils import atomic_output_path, validate_numeric_raster
from .earth_engine import configured_region, initialize


def run(ctx: RunContext, logger: logging.Logger) -> None:
        output = ctx.raw("flood", f"{ctx.config.iso3}_flood.tif")
    if output.is_file():
        try:
            metadata = validate_numeric_raster(output)
            logger.info(
                "reusing validated flood raster dimensions=%sx%s nodata=%s",
                metadata["width"],
                metadata["height"],
                metadata["nodata"],
                extra={"action": "reuse", "domain": "flood", "path": str(output)},
            )
            return
        except ValueError as exc:
            logger.warning(
                "existing flood raster is invalid and will be replaced error=%s",
                exc,
                extra={"action": "validate", "domain": "flood", "path": str(output)},
            )

    with logged_action(logger, "extract", domain="flood", path=str(output)):
        ee = initialize(ctx)
        region = configured_region(ctx, ee)
        flood = (
            ee.ImageCollection("WRI/Aqueduct_Flood_Hazard_Maps/V2")
            .filter(ee.Filter.eq("climatescenario", "rcp8p5"))
            .filter(ee.Filter.eq("floodtype", "inunriver"))
            .filter(ee.Filter.eq("returnperiod", 100))
            .filter(ee.Filter.eq("model", "0000GFDL-ESM2M"))
            .filter(ee.Filter.eq("year", 2030))
            .select("inundation_depth")
        )
        image = flood.sum().clip(region)
        import geemap
        download_threads = int(ctx.config.source("earth_engine").get("download_threads", 4))

        with atomic_output_path(output) as temporary:
            geemap.download_ee_image(
                image=image,
                filename=str(temporary),
                region=region,
                scale=1000,
                crs="EPSG:4326",
                dtype="float32",
                num_threads=download_threads,
                max_tile_size=32,
                max_tile_dim=10000,
                unmask_value=-9999,
                overwrite=True,
            )
            validate_numeric_raster(temporary)
