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
from ...domains.land_cover_contract import (
    ADMIN_AREA_COLUMN,
    DYNAMIC_WORLD_CLASSES,
    GEE_REDUCE_REGIONS_BACKEND,
    NODATA_CLASS,
    count_table_path,
    land_cover_backend,
    normalize_class_counts,
    raster_path,
    validate_admin_keys,
)
from ...geo import load_admin2
from ...io_utils import require_files
from ...logging_utils import logged_action
from ...raster_utils import validate_categorical_raster


def derive_indicators(
    class_counts,
    admin1: str,
    admin2: str,
    baseline_year: int,
    output_years: list[int],
):
    """Derive class-share changes and area-weighted agricultural land."""
    import numpy as np
    import pandas as pd

    frame = class_counts.copy()
    class_columns = list(DYNAMIC_WORLD_CLASSES.values())
    if ADMIN_AREA_COLUMN not in frame:
        raise ValueError(f"Land-cover class counts are missing {ADMIN_AREA_COLUMN!r}")
    frame[class_columns] = frame[class_columns].fillna(0)

    # The notebook currently uses mean(axis=1). Sum is the intended denominator
    # for a class share. The factor would cancel in the relative-change formula,
    # but sum makes built_pct and tree_pct meaningful proportions.
    frame["total"] = frame[class_columns].sum(axis=1)
    frame["built_pct"] = np.where(frame["total"] > 0, frame["built"] / frame["total"], np.nan)
    frame["tree_pct"] = np.where(frame["total"] > 0, frame["tree"] / frame["total"], np.nan)

    baseline = frame[frame["year"] == baseline_year][
        [admin1, admin2, "built_pct", "tree_pct"]
    ].rename(columns={"built_pct": "built_pct_baseline", "tree_pct": "tree_pct_baseline"})
    if baseline.duplicated([admin1, admin2]).any():
        raise ValueError("Land-cover baseline has duplicate administrative keys")

    output = frame[frame["year"].isin(output_years)].merge(
        baseline, on=[admin1, admin2], how="left", validate="many_to_one"
    )
    output["built_pct_change"] = np.where(
        output["built_pct_baseline"] > 0,
        100.0 * (output["built_pct"] - output["built_pct_baseline"]) / output["built_pct_baseline"],
        np.nan,
    )
    output["tree_pct_change"] = np.where(
        output["tree_pct_baseline"] > 0,
        100.0 * (output["tree_pct"] - output["tree_pct_baseline"]) / output["tree_pct_baseline"],
        np.nan,
    )
    # The extracted rasters use EPSG:4326, whose pixel area varies by latitude.
    # Applying a nominal 10 x 10 metre area to every pixel overstates land area.
    # Use the categorical crop share and the municipality's equal-area geometry
    # so agricultural area remains physically consistent with total land area.
    output["agri_land"] = np.where(
        output["total"] > 0,
        output["crops"] / output["total"] * output[ADMIN_AREA_COLUMN],
        np.nan,
    )
    return output[[admin1, admin2, "year", "built_pct_change", "tree_pct_change", "agri_land"]]


def run(ctx: RunContext, logger: logging.Logger) -> None:
    import pandas as pd

    admin2 = load_admin2(ctx.config)
    for column in (ctx.config.admin1, ctx.config.admin2):
        admin2[column] = admin2[column].astype(str)
    years = ctx.config.years("land_cover")
    output_years = ctx.config.years("indicators")
    baseline_year = years[0]
    backend = land_cover_backend(ctx.config)
    rows = []
    state_dir = ctx.config.workspace / "state" / "land_cover"

    with logged_action(logger, "process", domain="land_cover"):
        for item_number, year in enumerate(years, start=1):
            if backend == GEE_REDUCE_REGIONS_BACKEND:
                counts = count_table_path(ctx.config, year)
                require_files([counts], "land-cover count tables")
                with logged_action(
                    logger,
                    "load_counts",
                    domain="land_cover",
                    phase=str(year),
                    path=str(counts),
                ):
                    frame = pd.read_csv(counts)
                action = "count_table"
            else:
                from rasterstats import zonal_stats

                raster = raster_path(ctx.config, year)
                require_files([raster], "land-cover rasters")
                validate_categorical_raster(
                    raster,
                    valid_classes=set(DYNAMIC_WORLD_CLASSES),
                    expected_nodata=NODATA_CLASS,
                )
                checkpoint = state_dir / f"class_counts_{year}.parquet"
                manifest = checkpoint.with_suffix(".manifest.json")
                expected = {
                    "algorithm": "dynamic-world-zonal-counts-v2",
                    "raster": path_signature(raster),
                    "boundary": path_signature(
                        ctx.config.boundary_path("admin2"), shapefile_family=True
                    ),
                    "classes": DYNAMIC_WORLD_CLASSES,
                    "nodata": NODATA_CLASS,
                }
                if checkpoint_matches(checkpoint, manifest, expected):
                    frame = pd.read_parquet(checkpoint)
                    logger.info(
                        "reused land-cover checkpoint year=%d item=%d/%d rows=%d",
                        year,
                        item_number,
                        len(years),
                        len(frame),
                        extra={"action": "checkpoint_reuse", "domain": "land_cover", "phase": str(year), "path": str(checkpoint)},
                    )
                else:
                    with logged_action(
                        logger,
                        "zonal_counts",
                        domain="land_cover",
                        phase=str(year),
                        path=str(raster),
                    ):
                        stats = zonal_stats(
                            admin2.geometry,
                            raster,
                            categorical=True,
                            nodata=NODATA_CLASS,
                        )
                        frame = admin2[[ctx.config.admin1, ctx.config.admin2]].copy()
                        frame["year"] = year
                        for class_id, class_name in DYNAMIC_WORLD_CLASSES.items():
                            frame[class_name] = [float(item.get(class_id, 0)) for item in stats]
                        write_frame_parquet_atomic(frame, checkpoint)
                        write_checkpoint_manifest(manifest, expected, rows=len(frame))
                action = "raster_counts"

            frame = normalize_class_counts(
                frame,
                admin1=ctx.config.admin1,
                admin2=ctx.config.admin2,
                expected_year=year,
            )
            validate_admin_keys(
                frame,
                admin2,
                admin1=ctx.config.admin1,
                admin2=ctx.config.admin2,
            )
            logger.info(
                "validated land-cover class counts backend=%s year=%d item=%d/%d rows=%d",
                backend,
                year,
                item_number,
                len(years),
                len(frame),
                extra={"action": action, "domain": "land_cover", "phase": str(year)},
            )
            rows.append(frame)

        class_counts = pd.concat(rows, ignore_index=True)
        area_lookup = admin2[[ctx.config.admin1, ctx.config.admin2]].copy()
        area_lookup[ADMIN_AREA_COLUMN] = (
            admin2.to_crs("EPSG:6933").geometry.area / 1_000_000.0
        )
        if (area_lookup[ADMIN_AREA_COLUMN] <= 0).any():
            raise ValueError("Admin-2 boundary contains a non-positive land area")
        class_counts = class_counts.merge(
            area_lookup,
            on=[ctx.config.admin1, ctx.config.admin2],
            how="left",
            validate="many_to_one",
        )
        if class_counts[ADMIN_AREA_COLUMN].isna().any():
            raise ValueError("Land-cover class counts could not be matched to every admin-2 area")
        output = derive_indicators(
            class_counts,
            ctx.config.admin1,
            ctx.config.admin2,
            baseline_year,
            output_years,
        )
        write_frame_csv_atomic(output, ctx.output("lulc.csv"))
        logger.info("land-cover rows=%d", len(output), extra={"domain": "land_cover"})
