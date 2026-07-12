from __future__ import annotations

import logging

from ...checkpoint_utils import (
    checkpoint_matches,
    path_signature,
    write_checkpoint_manifest,
    write_frame_csv_atomic,
    write_frame_parquet_atomic,
)
from ...context import RunContext
from ...geo import load_admin2
from ...io_utils import require_files
from ...logging_utils import logged_action


def run(ctx: RunContext, logger: logging.Logger) -> None:
    import pandas as pd
    from rasterstats import zonal_stats

    admin2 = load_admin2(ctx.config)
    rows = []
    state_dir = ctx.config.workspace / "state" / "luminosity"
    boundary_signature = path_signature(
        ctx.config.boundary_path("admin2"), shapefile_family=True
    )
    with logged_action(logger, "process", domain="luminosity"):
        years = ctx.config.years("indicators")
        for item_number, year in enumerate(years, start=1):
            annual_raster = ctx.raw("luminosity", f"{ctx.config.iso3}_{year}_annual.tif")
            if annual_raster.is_file():
                rasters = [annual_raster]
                source_kind = "annual"
            else:
                rasters = [
                    ctx.raw("luminosity", f"{ctx.config.iso3}_{year}-{month:02d}.tif")
                    for month in range(1, 13)
                ]
                require_files(rasters, "luminosity rasters")
                source_kind = "monthly-fallback"

            checkpoint = state_dir / f"annual_{year}.parquet"
            manifest = checkpoint.with_suffix(".manifest.json")
            expected = {
                "algorithm": "viirs-annual-zonal-sum-v2",
                "rasters": [path_signature(path) for path in rasters],
                "boundary": boundary_signature,
            }
            if checkpoint_matches(checkpoint, manifest, expected):
                annual = pd.read_parquet(checkpoint)
                logger.info(
                    "reused luminosity checkpoint year=%d item=%d/%d source=%s rows=%d",
                    year,
                    item_number,
                    len(years),
                    source_kind,
                    len(annual),
                    extra={"action": "checkpoint_reuse", "domain": "luminosity", "phase": str(year), "path": str(checkpoint)},
                )
            else:
                annual = admin2[[ctx.config.admin1, ctx.config.admin2]].copy()
                annual["luminosity"] = 0.0
                for raster_number, raster in enumerate(rasters, start=1):
                    require_files([raster], "luminosity rasters")
                    stats = zonal_stats(admin2.geometry, raster, stats=["sum"])
                    annual["luminosity"] += [float(item.get("sum") or 0) for item in stats]
                    logger.info(
                        "luminosity raster processed year=%d raster=%d/%d source=%s",
                        year,
                        raster_number,
                        len(rasters),
                        source_kind,
                        extra={"action": "zonal_sum", "domain": "luminosity", "phase": str(year), "path": str(raster)},
                    )
                annual["year"] = year
                write_frame_parquet_atomic(annual, checkpoint)
                write_checkpoint_manifest(manifest, expected, rows=len(annual))
            rows.append(annual)
        output = pd.concat(rows, ignore_index=True)
        write_frame_csv_atomic(output, ctx.output("luminosity.csv"))
        logger.info("luminosity rows=%d", len(output), extra={"domain": "luminosity"})
