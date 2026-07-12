from __future__ import annotations

import logging

from .checkpoint_utils import (
    checkpoint_matches,
    path_signature,
    write_checkpoint_manifest,
    write_frame_csv_atomic,
    write_frame_parquet_atomic,
)
from .context import RunContext
from .geo import load_admin2, osm_layer, read_osm_filtered
from .io_utils import require_files
from .logging_utils import logged_action
from .raster_utils import atomic_output_path
from .transport_utils import (
    add_length_km,
    assign_and_clip_lines,
    read_checkpoint_totals,
    read_transport_source,
    transport_checkpoint_inputs,
    validate_checkpoint_file,
    write_checkpoint,
)

PREREQUISITES = ("key_assets", "transport", "population")


def run(ctx: RunContext, name: str, logger: logging.Logger) -> None:
    if name == "key_assets":
        key_assets(ctx, logger)
    elif name == "transport":
        transport(ctx, logger)
    elif name == "population":
        population(ctx, logger)
    else:
        raise ValueError(f"Unknown prerequisite: {name}")


def key_assets(ctx: RunContext, logger: logging.Logger) -> None:
    import geopandas as gpd
    import pyogrio
    import shapely

    source = osm_layer(ctx.config, "gis_osm_buildings_a_free_1.shp")
    require_files([source], "OSM building layer")
    parquet_output = ctx.config.shape_dir / "assets.parquet"
    geojson_output = ctx.config.shape_dir / "assets.geojson"
    manifest = parquet_output.with_suffix(".manifest.json")
    available = set(pyogrio.read_info(source)["fields"])
    type_column = "type" if "type" in available else "fclass"
    categories = {"school", "university", "hospital"}
    expected = {
        "algorithm": "osm-key-assets-filter-v2",
        "source": path_signature(source, shapefile_family=True),
        "category_column": type_column,
        "categories": sorted(categories),
        "year": int(ctx.config.data["years"]["transport"]) - 1,
    }
    with logged_action(logger, "process", domain="key_assets", path=str(parquet_output)):
        if checkpoint_matches(parquet_output, manifest, expected):
            assets = gpd.read_parquet(parquet_output)
            logger.info(
                "reused key-assets checkpoint rows=%d",
                len(assets),
                extra={"action": "checkpoint_reuse", "domain": "key_assets", "path": str(parquet_output)},
            )
        else:
            assets = read_osm_filtered(
                source,
                category_column=type_column,
                categories=categories,
                columns=["osm_id", "name", type_column],
                case_insensitive=False,
            ).rename(columns={type_column: "amenity"}).to_crs("EPSG:4326")
            assets["year"] = int(ctx.config.data["years"]["transport"]) - 1
            centroids = shapely.centroid(assets.geometry.array)
            assets["lat"] = shapely.get_y(centroids)
            assets["lon"] = shapely.get_x(centroids)
            assets["bounding_box"] = assets.geometry.bounds.to_numpy().tolist()
            keep = [
                column
                for column in (
                    "osm_id",
                    "name",
                    "amenity",
                    "year",
                    "lat",
                    "lon",
                    "bounding_box",
                    "geometry",
                )
                if column in assets.columns
            ]
            assets = assets[keep]
            write_frame_parquet_atomic(assets, parquet_output)
            write_checkpoint_manifest(manifest, expected, rows=len(assets))
        if not geojson_output.is_file() or geojson_output.stat().st_mtime_ns < parquet_output.stat().st_mtime_ns:
            with atomic_output_path(geojson_output) as temporary:
                assets.to_file(temporary, driver="GeoJSON", index=False)
        logger.info("asset rows=%d", len(assets), extra={"domain": "key_assets"})


def transport(ctx: RunContext, logger: logging.Logger) -> None:
    roads_path = osm_layer(ctx.config, "gis_osm_roads_free_1.shp")
    rails_path = osm_layer(ctx.config, "gis_osm_railways_free_1.shp")
    require_files([roads_path, rails_path], "OSM transport layers")
    year = int(ctx.config.data["years"]["transport"])
    settings = ctx.config.data.get("processing", {}).get("transport", {})
    metric_crs = str(settings.get("metric_crs", "EPSG:6933"))
    resume = bool(settings.get("resume_from_checkpoints", True))
    road_out = ctx.config.shape_dir / f"roads_intersect_{year}.parquet"
    rail_out = ctx.config.shape_dir / f"rails_intersect_{year}.parquet"

    valid = {
        "residential", "service", "track", "unclassified", "secondary", "tertiary",
        "track_grade1", "track_grade2", "track_grade3", "track_grade4", "track_grade5",
        "primary", "motorway", "motorway_link", "living_street", "primary_link",
        "secondary_link", "tertiary_link", "trunk", "trunk_link",
    }
    road_inputs = transport_checkpoint_inputs(
        source=roads_path,
        boundary=ctx.config.boundary_path("admin2"),
        allowed_classes=valid,
        metric_crs=metric_crs,
    )
    rail_inputs = transport_checkpoint_inputs(
        source=rails_path,
        boundary=ctx.config.boundary_path("admin2"),
        allowed_classes=None,
        metric_crs=metric_crs,
    )

    with logged_action(logger, "process", domain="transport", phase=str(year)):
        with logged_action(logger, "load_boundaries", domain="transport", phase=str(year)):
            admin2 = load_admin2(ctx.config)
            logger.info(
                "admin2 boundaries loaded rows=%d crs=%s",
                len(admin2),
                admin2.crs,
                extra={"action": "load_boundaries", "domain": "transport", "phase": str(year)},
            )

        roads_reused = False
        if resume and road_out.is_file():
            with logged_action(
                logger, "load_roads_checkpoint", domain="transport", phase=str(year), path=str(road_out)
            ):
                try:
                    validate_checkpoint_file(
                        road_out,
                        admin1_column=ctx.config.admin1,
                        admin2_column=ctx.config.admin2,
                        expected_inputs=road_inputs,
                    )
                    road_totals, road_count = read_checkpoint_totals(
                        road_out,
                        admin1_column=ctx.config.admin1,
                        admin2_column=ctx.config.admin2,
                    )
                    road_totals = road_totals.rename(columns={"length_km": "road_length"})
                    roads_reused = True
                except ValueError as exc:
                    logger.warning(
                        "road checkpoint will be rebuilt reason=%s",
                        exc,
                        extra={"action": "checkpoint_invalid", "domain": "transport", "phase": str(year), "path": str(road_out)},
                    )
        if not roads_reused:
            with logged_action(
                logger, "load_and_filter_roads", domain="transport", phase=str(year), path=str(roads_path)
            ):
                roads = read_transport_source(roads_path, allowed_classes=valid).to_crs(admin2.crs)
                logger.info(
                    "filtered roads loaded rows=%d",
                    len(roads),
                    extra={"action": "load_and_filter_roads", "domain": "transport", "phase": str(year)},
                )
            with logged_action(logger, "assign_and_clip_roads", domain="transport", phase=str(year)):
                roads = assign_and_clip_lines(
                    roads,
                    admin2,
                    admin1_column=ctx.config.admin1,
                    admin2_column=ctx.config.admin2,
                    logger=logger,
                    label="roads",
                )
                logger.info(
                    "road assignment completed rows=%d",
                    len(roads),
                    extra={"action": "assign_and_clip_roads", "domain": "transport", "phase": str(year)},
                )
            with logged_action(logger, "calculate_road_lengths", domain="transport", phase=str(year)):
                roads = add_length_km(roads, metric_crs)
            with logged_action(
                logger, "write_roads_checkpoint", domain="transport", phase=str(year), path=str(road_out)
            ):
                write_checkpoint(roads, road_out, inputs=road_inputs)

            road_count = len(roads)
            road_totals = roads.groupby(
                [ctx.config.admin1, ctx.config.admin2], as_index=False
            )["length_km"].sum().rename(columns={"length_km": "road_length"})
            del roads

        rails_reused = False
        if resume and rail_out.is_file():
            with logged_action(
                logger, "load_rails_checkpoint", domain="transport", phase=str(year), path=str(rail_out)
            ):
                try:
                    validate_checkpoint_file(
                        rail_out,
                        admin1_column=ctx.config.admin1,
                        admin2_column=ctx.config.admin2,
                        expected_inputs=rail_inputs,
                    )
                    rail_totals, rail_count = read_checkpoint_totals(
                        rail_out,
                        admin1_column=ctx.config.admin1,
                        admin2_column=ctx.config.admin2,
                    )
                    rail_totals = rail_totals.rename(columns={"length_km": "rail_length"})
                    rails_reused = True
                except ValueError as exc:
                    logger.warning(
                        "rail checkpoint will be rebuilt reason=%s",
                        exc,
                        extra={"action": "checkpoint_invalid", "domain": "transport", "phase": str(year), "path": str(rail_out)},
                    )
        if not rails_reused:
            with logged_action(
                logger, "load_rails", domain="transport", phase=str(year), path=str(rails_path)
            ):
                rails = read_transport_source(rails_path).to_crs(admin2.crs)
                logger.info(
                    "rails loaded rows=%d",
                    len(rails),
                    extra={"action": "load_rails", "domain": "transport", "phase": str(year)},
                )
            with logged_action(logger, "assign_and_clip_rails", domain="transport", phase=str(year)):
                rails = assign_and_clip_lines(
                    rails,
                    admin2,
                    admin1_column=ctx.config.admin1,
                    admin2_column=ctx.config.admin2,
                    logger=logger,
                    label="rails",
                )
                logger.info(
                    "rail assignment completed rows=%d",
                    len(rails),
                    extra={"action": "assign_and_clip_rails", "domain": "transport", "phase": str(year)},
                )
            with logged_action(logger, "calculate_rail_lengths", domain="transport", phase=str(year)):
                rails = add_length_km(rails, metric_crs)
            with logged_action(
                logger, "write_rails_checkpoint", domain="transport", phase=str(year), path=str(rail_out)
            ):
                write_checkpoint(rails, rail_out, inputs=rail_inputs)

            rail_count = len(rails)
            rail_totals = rails.groupby(
                [ctx.config.admin1, ctx.config.admin2], as_index=False
            )["length_km"].sum().rename(columns={"length_km": "rail_length"})
            del rails

        output = ctx.output(f"{ctx.config.iso3}_infra_length.csv")
        with logged_action(
            logger, "aggregate_and_write_lengths", domain="transport", phase=str(year), path=str(output)
        ):
            totals = road_totals.merge(
                rail_totals, on=[ctx.config.admin1, ctx.config.admin2], how="outer"
            ).fillna(0)
            totals["year"] = int(ctx.config.data["years"]["static_merge"])
            totals["transport_source_year"] = year
            write_frame_csv_atomic(totals, output)
            logger.info(
                "transport totals written rows=%d road_rows=%d rail_rows=%d",
                len(totals),
                road_count,
                rail_count,
                extra={
                    "action": "aggregate_and_write_lengths",
                    "domain": "transport",
                    "phase": str(year),
                    "path": str(output),
                },
            )


def _population_label_grid(admin2, raster_path):
    import numpy as np
    import rasterio
    from rasterio.features import rasterize

    with rasterio.open(raster_path) as dataset:
        if dataset.crs is None:
            raise ValueError(f"Population raster has no CRS: {raster_path}")
        projected = admin2.to_crs(dataset.crs)
        if len(projected) > np.iinfo(np.int32).max:
            raise ValueError("Too many administrative regions for population label grid")
        label_dtype = "int16" if len(projected) <= np.iinfo(np.int16).max else "int32"
        labels = rasterize(
            ((geometry, index) for index, geometry in enumerate(projected.geometry)),
            out_shape=(dataset.height, dataset.width),
            transform=dataset.transform,
            fill=-1,
            all_touched=False,
            dtype=label_dtype,
        )
        reference = {
            "width": dataset.width,
            "height": dataset.height,
            "transform": dataset.transform,
            "crs": dataset.crs,
        }
    return labels, reference


def _population_block_sums(
    raster_path,
    labels,
    reference,
    *,
    region_count: int,
    logger: logging.Logger | None = None,
):
    import numpy as np
    import rasterio

    sums = np.zeros(region_count, dtype="float64")
    counts = np.zeros(region_count, dtype="int64")
    with rasterio.open(raster_path) as dataset:
        if (
            dataset.width != reference["width"]
            or dataset.height != reference["height"]
            or dataset.transform != reference["transform"]
            or dataset.crs != reference["crs"]
        ):
            raise ValueError(
                f"Population rasters are not aligned; cannot reuse boundary labels: {raster_path}"
            )
        windows = list(dataset.block_windows(1))
        for item_number, (_, window) in enumerate(windows, start=1):
            values = dataset.read(1, window=window, masked=True)
            row_slice, column_slice = window.toslices()
            block_labels = labels[row_slice, column_slice]
            data = np.asarray(values.data)
            valid = (
                (block_labels >= 0)
                & ~np.ma.getmaskarray(values)
                & np.isfinite(data)
            )
            if valid.any():
                selected_labels = block_labels[valid]
                sums += np.bincount(
                    selected_labels,
                    weights=data[valid],
                    minlength=region_count,
                )[:region_count]
                counts += np.bincount(
                    selected_labels,
                    minlength=region_count,
                )[:region_count]
            if logger and (item_number % 25 == 0 or item_number == len(windows)):
                logger.info(
                    "population raster progress blocks=%d/%d",
                    item_number,
                    len(windows),
                    extra={"action": "block_sum", "domain": "population", "path": str(raster_path)},
                )
    sums[counts == 0] = np.nan
    return sums


def population(ctx: RunContext, logger: logging.Logger) -> None:
    import pandas as pd

    admin2 = load_admin2(ctx.config)
    rows = []
    state_dir = ctx.config.workspace / "state" / "population"
    boundary_signature = path_signature(
        ctx.config.boundary_path("admin2"), shapefile_family=True
    )
    labels = reference = None
    with logged_action(logger, "process", domain="population"):
        years = ctx.config.years("population")
        for item_number, year in enumerate(years, start=1):
            raster = ctx.raw("population", f"{ctx.config.iso3.lower()}_pop_{year}_CN_100m_R2025A_v1.tif")
            require_files([raster], "WorldPop raster")
            checkpoint = state_dir / f"admin2_{year}.parquet"
            manifest = checkpoint.with_suffix(".manifest.json")
            expected = {
                "algorithm": "worldpop-block-zonal-sum-v3",
                "raster": path_signature(raster),
                "boundary": boundary_signature,
            }
            if checkpoint_matches(checkpoint, manifest, expected):
                frame = pd.read_parquet(checkpoint)
                logger.info(
                    "reused population checkpoint year=%d item=%d/%d rows=%d",
                    year,
                    item_number,
                    len(years),
                    len(frame),
                    extra={"action": "checkpoint_reuse", "domain": "population", "phase": str(year), "path": str(checkpoint)},
                )
            else:
                if labels is None or reference is None:
                    with logged_action(
                        logger,
                        "rasterize_admin_labels",
                        domain="population",
                        path=str(raster),
                    ):
                        labels, reference = _population_label_grid(admin2, raster)
                frame = admin2[[ctx.config.admin1, ctx.config.admin2]].copy()
                frame["year"] = year
                frame["population_total"] = _population_block_sums(
                    raster,
                    labels,
                    reference,
                    region_count=len(admin2),
                    logger=logger,
                )
                write_frame_parquet_atomic(frame, checkpoint)
                write_checkpoint_manifest(manifest, expected, rows=len(frame))
            rows.append(frame)
        output = pd.concat(rows, ignore_index=True)
        write_frame_csv_atomic(output, ctx.output(f"{ctx.config.iso3}_population.csv"))
        logger.info("population rows=%d", len(output), extra={"domain": "population"})
