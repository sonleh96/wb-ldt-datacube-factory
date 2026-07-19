from __future__ import annotations

import logging
import time
from typing import Any

from ...checkpoint_utils import (
    checkpoint_matches,
    fingerprint,
    path_signature,
    read_manifest,
    write_checkpoint_manifest,
    write_frame_csv_atomic,
)
from ...context import RunContext
from ...domains.land_cover_contract import (
    DYNAMIC_WORLD_CLASSES,
    GEE_ALGORITHM_VERSION,
    GEE_REDUCE_REGIONS_BACKEND,
    NODATA_CLASS,
    count_manifest_path,
    count_table_path,
    gee_task_path,
    intermediate_asset_prefix,
    land_cover_backend,
    normalize_class_counts,
    raster_path,
    validate_admin_keys,
)
from ...geo import load_admin2
from ...logging_utils import logged_action
from ...raster_utils import atomic_output_path, validate_categorical_raster
from .earth_engine import configured_region, initialize


def _annual_dynamic_world(ee, year: int):
    return (
        ee.ImageCollection("GOOGLE/DYNAMICWORLD/V1")
        .filterDate(f"{year}-01-01", f"{year + 1}-01-01")
        .select("label")
        .mode()
    )


def _run_raster_download(ctx: RunContext, logger: logging.Logger, ee) -> None:
    region = configured_region(ctx, ee)
    pixel_size_m = float(ctx.config.source("land_cover").get("pixel_size_m", 10))
    download_threads = int(ctx.config.source("earth_engine").get("download_threads", 4))
    import geemap

    for year in ctx.config.years("land_cover"):
        destination = raster_path(ctx.config, year)
        if destination.is_file():
            try:
                metadata = validate_categorical_raster(
                    destination,
                    valid_classes=set(DYNAMIC_WORLD_CLASSES),
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
                _annual_dynamic_world(ee, year)
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
                    valid_classes=set(DYNAMIC_WORLD_CLASSES),
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


def _gee_expected_inputs(ctx: RunContext, year: int) -> dict[str, Any]:
    source = ctx.config.source("land_cover")
    earth_engine = ctx.config.source("earth_engine")
    return {
        "algorithm": GEE_ALGORITHM_VERSION,
        "year": year,
        "dynamic_world_collection": "GOOGLE/DYNAMICWORLD/V1",
        "admin2_asset_id": str(earth_engine["admin2_asset_id"]),
        "admin1": ctx.config.admin1,
        "admin2": ctx.config.admin2,
        "local_boundary": path_signature(
            ctx.config.boundary_path("admin2"), shapefile_family=True
        ),
        "classes": DYNAMIC_WORLD_CLASSES,
        "scale": float(source.get("pixel_size_m", 10)),
        "crs": "EPSG:4326",
        "tile_scale": float(source.get("tile_scale", 4)),
        "max_pixels_per_region": int(
            source.get("max_pixels_per_region", 1_000_000_000)
        ),
        "reducer": "sum_unweighted",
    }


def _intermediate_asset_id(ctx: RunContext, year: int, expected: dict[str, Any]) -> str:
    project_id = str(ctx.config.source("earth_engine")["project_id"]).strip()
    prefix = intermediate_asset_prefix(ctx.config)
    digest = fingerprint(expected)[:12]
    return f"projects/{project_id}/assets/{prefix}_{ctx.config.iso3}_{year}_{digest}"


def _build_reduced_collection(ctx: RunContext, ee, year: int):
    source = ctx.config.source("land_cover")
    boundary_asset = str(ctx.config.source("earth_engine")["admin2_asset_id"])
    boundaries = ee.FeatureCollection(boundary_asset).select(
        [ctx.config.admin1, ctx.config.admin2]
    )
    labels = _annual_dynamic_world(ee, year)
    class_image = ee.Image.cat(
        [
            labels.eq(class_id).rename(class_name).toUint8()
            for class_id, class_name in DYNAMIC_WORLD_CLASSES.items()
        ]
    ).updateMask(labels.mask())
    reduced = class_image.reduceRegions(
        collection=boundaries,
        # Centroid-based inclusion is the closest Earth Engine equivalent to
        # the local categorical rasterstats backend.
        reducer=ee.Reducer.sum().unweighted(),
        scale=float(source.get("pixel_size_m", 10)),
        crs="EPSG:4326",
        tileScale=float(source.get("tile_scale", 4)),
        maxPixelsPerRegion=int(source.get("max_pixels_per_region", 1_000_000_000)),
    )

    properties = [ctx.config.admin1, ctx.config.admin2, *DYNAMIC_WORLD_CLASSES.values()]

    def without_geometry(feature):
        values = {name: feature.get(name) for name in properties}
        values["year"] = year
        return ee.Feature(None, values)

    return reduced.map(without_geometry)


def _asset_exists(ee, asset_id: str) -> bool:
    try:
        ee.data.getAsset(asset_id)
        return True
    except Exception as error:
        message = str(error).lower()
        if "not found" in message or "does not exist" in message or "404" in message:
            return False
        raise


def _task_status(ee, task_id: str) -> dict[str, Any]:
    records = ee.data.getTaskStatus(task_id)
    if not records:
        raise RuntimeError(f"Earth Engine returned no status for task {task_id}")
    record = records[0]
    if not isinstance(record, dict):
        raise RuntimeError(f"Earth Engine returned an invalid status for task {task_id}")
    return record


def _wait_for_task(
    ctx: RunContext,
    logger: logging.Logger,
    ee,
    *,
    task_id: str,
    asset_id: str,
    task_state_path,
    expected: dict[str, Any],
) -> None:
    poll_seconds = min(
        60.0,
        float(ctx.config.source("land_cover").get("poll_seconds", 20)),
    )
    while True:
        status = _task_status(ee, task_id)
        state = str(status.get("state", "UNKNOWN")).upper()
        write_checkpoint_manifest(
            task_state_path,
            expected,
            task_id=task_id,
            task_state=state,
            asset_id=asset_id,
        )
        logger.info(
            "Earth Engine land-cover task year=%s state=%s task_id=%s",
            expected["year"],
            state,
            task_id,
            extra={
                "action": "poll",
                "domain": "land_cover",
                "phase": str(expected["year"]),
                "path": asset_id,
                "status": state.lower(),
            },
        )
        if state == "COMPLETED":
            return
        if state in {"FAILED", "CANCELLED", "CANCEL_REQUESTED"}:
            detail = status.get("error_message") or status.get("error_details") or "no error detail"
            raise RuntimeError(
                f"Earth Engine land-cover task {task_id} ended in {state}: {detail}"
            )
        if state not in {"READY", "RUNNING"}:
            raise RuntimeError(
                f"Earth Engine land-cover task {task_id} returned unexpected state {state}"
            )
        time.sleep(poll_seconds)


def _submit_or_resume_export(
    ctx: RunContext,
    logger: logging.Logger,
    ee,
    *,
    year: int,
    asset_id: str,
    task_state_path,
    expected: dict[str, Any],
) -> str | None:
    if _asset_exists(ee, asset_id):
        logger.info(
            "reusing completed Earth Engine land-cover asset year=%d",
            year,
            extra={"action": "asset_reuse", "domain": "land_cover", "phase": str(year), "path": asset_id},
        )
        return None

    recorded = read_manifest(task_state_path)
    task_id = None
    if recorded.get("fingerprint") == fingerprint(expected):
        task_id = recorded.get("task_id")
    if task_id:
        status = _task_status(ee, str(task_id))
        state = str(status.get("state", "UNKNOWN")).upper()
        if state in {"READY", "RUNNING"}:
            logger.info(
                "resuming Earth Engine land-cover task year=%d task_id=%s",
                year,
                task_id,
                extra={"action": "task_resume", "domain": "land_cover", "phase": str(year), "path": asset_id, "status": state.lower()},
            )
            _wait_for_task(
                ctx,
                logger,
                ee,
                task_id=str(task_id),
                asset_id=asset_id,
                task_state_path=task_state_path,
                expected=expected,
            )
            return str(task_id)
        if state == "COMPLETED" and _asset_exists(ee, asset_id):
            return str(task_id)
        logger.warning(
            "recorded Earth Engine land-cover task cannot be resumed year=%d state=%s; submitting a replacement",
            year,
            state,
            extra={"action": "task_replace", "domain": "land_cover", "phase": str(year), "path": asset_id, "status": state.lower()},
        )

    collection = _build_reduced_collection(ctx, ee, year)
    description = f"ldt_land_cover_{ctx.config.iso3}_{year}_{fingerprint(expected)[:8]}"
    task = ee.batch.Export.table.toAsset(
        collection=collection,
        description=description,
        assetId=asset_id,
    )
    task.start()
    if not task.id:
        raise RuntimeError("Earth Engine did not return an ID for the submitted land-cover task")
    task_id = str(task.id)
    write_checkpoint_manifest(
        task_state_path,
        expected,
        task_id=task_id,
        task_state="SUBMITTED",
        asset_id=asset_id,
    )
    logger.info(
        "submitted Earth Engine land-cover task year=%d task_id=%s",
        year,
        task_id,
        extra={"action": "submit", "domain": "land_cover", "phase": str(year), "path": asset_id, "status": "submitted"},
    )
    _wait_for_task(
        ctx,
        logger,
        ee,
        task_id=task_id,
        asset_id=asset_id,
        task_state_path=task_state_path,
        expected=expected,
    )
    return task_id


def _retrieve_count_table(ee, asset_id: str):
    import pandas as pd

    result = ee.data.computeFeatures(
        {
            "expression": ee.FeatureCollection(asset_id),
            "fileFormat": "PANDAS_DATAFRAME",
        }
    )
    return result.copy() if isinstance(result, pd.DataFrame) else pd.DataFrame(result)


def _run_gee_reduce_regions(ctx: RunContext, logger: logging.Logger, ee) -> None:
    import pandas as pd

    admin2 = load_admin2(ctx.config)
    source = ctx.config.source("land_cover")
    cleanup_assets = bool(source.get("cleanup_intermediate_assets", False))

    for year in ctx.config.years("land_cover"):
        destination = count_table_path(ctx.config, year)
        manifest = count_manifest_path(ctx.config, year)
        task_state_path = gee_task_path(ctx.config, year)
        expected = _gee_expected_inputs(ctx, year)
        asset_id = _intermediate_asset_id(ctx, year, expected)

        if checkpoint_matches(destination, manifest, expected):
            try:
                frame = normalize_class_counts(
                    pd.read_csv(destination),
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
                    "reusing validated land-cover count table year=%d rows=%d",
                    year,
                    len(frame),
                    extra={"action": "reuse", "domain": "land_cover", "phase": str(year), "path": str(destination)},
                )
                continue
            except ValueError as error:
                logger.warning(
                    "existing land-cover count table is invalid and will be replaced year=%d error=%s",
                    year,
                    error,
                    extra={"action": "validate", "domain": "land_cover", "phase": str(year), "path": str(destination)},
                )

        with logged_action(
            logger,
            "extract_reduce_regions",
            domain="land_cover",
            phase=str(year),
            path=str(destination),
        ):
            task_id = _submit_or_resume_export(
                ctx,
                logger,
                ee,
                year=year,
                asset_id=asset_id,
                task_state_path=task_state_path,
                expected=expected,
            )
            frame = normalize_class_counts(
                _retrieve_count_table(ee, asset_id),
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
            write_frame_csv_atomic(frame, destination)
            write_checkpoint_manifest(
                manifest,
                expected,
                rows=len(frame),
                task_id=task_id,
                task_state="COMPLETED",
                asset_id=asset_id,
                asset_deleted=False,
            )
            logger.info(
                "saved land-cover count table year=%d rows=%d",
                year,
                len(frame),
                extra={"action": "save", "domain": "land_cover", "phase": str(year), "path": str(destination)},
            )

            if cleanup_assets:
                try:
                    ee.data.deleteAsset(asset_id)
                    write_checkpoint_manifest(
                        manifest,
                        expected,
                        rows=len(frame),
                        task_id=task_id,
                        task_state="COMPLETED",
                        asset_id=asset_id,
                        asset_deleted=True,
                    )
                    logger.info(
                        "deleted intermediate Earth Engine land-cover asset year=%d",
                        year,
                        extra={"action": "asset_cleanup", "domain": "land_cover", "phase": str(year), "path": asset_id},
                    )
                except Exception as error:
                    logger.warning(
                        "could not delete intermediate Earth Engine land-cover asset year=%d error=%s",
                        year,
                        error,
                        extra={"action": "asset_cleanup", "domain": "land_cover", "phase": str(year), "path": asset_id, "status": "warning"},
                    )


def run(ctx: RunContext, logger: logging.Logger) -> None:
    backend = land_cover_backend(ctx.config)
    logger.info(
        "selected land-cover extraction backend=%s",
        backend,
        extra={"action": "backend", "domain": "land_cover", "phase": "extract"},
    )
    ee = initialize(ctx)
    if backend == GEE_REDUCE_REGIONS_BACKEND:
        _run_gee_reduce_regions(ctx, logger, ee)
        return
    _run_raster_download(ctx, logger, ee)
