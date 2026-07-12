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
from ...geo import load_admin2, osm_layer, read_osm_filtered
from ...io_utils import require_files
from ...logging_utils import logged_action

CATEGORIES = {
    "pois": {
        "hotel", "hostel", "guesthouse", "motel", "chalet", "camp_site",
        "attraction", "museum", "viewpoint", "monument", "memorial", "ruins",
        "archaeological", "theme_park", "zoo", "tourist_info", "restaurant",
        "bar", "cafe", "pub", "travel_agent", "car_rental", "fast_food",
    },
    "buildings": {"hotel", "lodge", "guest_house", "cabin", "static_caravan"},
    "landuse": {"nature_reserve", "park"},
    "transport": {"airport", "airfield", "helipad"},
}


def run(ctx: RunContext, logger: logging.Logger) -> None:
    import geopandas as gpd
    import pandas as pd

    admin2 = load_admin2(ctx.config)
    admin2 = admin2.to_crs("EPSG:4326")
    admin_columns = [ctx.config.admin1, ctx.config.admin2]
    specs = {
        "pois": ("gis_osm_pois_a_free_1.shp", "fclass"),
        "buildings": ("gis_osm_buildings_a_free_1.shp", "type"),
        "landuse": ("gis_osm_landuse_a_free_1.shp", "fclass"),
        "transport": ("gis_osm_transport_a_free_1.shp", "fclass"),
    }
    paths = [osm_layer(ctx.config, filename) for filename, _ in specs.values()]
    require_files(paths, "OSM tourism layers")
    state_dir = ctx.config.workspace / "state" / "tourism"
    boundary_signature = path_signature(
        ctx.config.boundary_path("admin2"), shapefile_family=True
    )
    layer_counts = []
    with logged_action(logger, "process", domain="tourism"):
        for name, (filename, column) in specs.items():
            source_path = osm_layer(ctx.config, filename)
            checkpoint = state_dir / f"{name}_counts.parquet"
            manifest = checkpoint.with_suffix(".manifest.json")
            expected = {
                "algorithm": "osm-tourism-representative-point-v2",
                "source": path_signature(source_path, shapefile_family=True),
                "boundary": boundary_signature,
                "column": column,
                "categories": sorted(CATEGORIES[name]),
            }
            if checkpoint_matches(checkpoint, manifest, expected):
                grouped = pd.read_parquet(checkpoint)
                logger.info(
                    "reused tourism layer checkpoint layer=%s rows=%d",
                    name,
                    len(grouped),
                    extra={"action": "checkpoint_reuse", "domain": "tourism", "phase": name, "path": str(checkpoint)},
                )
            else:
                selected = read_osm_filtered(
                    source_path,
                    category_column=column,
                    categories=CATEGORIES[name],
                    columns=[column],
                ).to_crs("EPSG:4326")
                if selected.empty:
                    grouped = pd.DataFrame(columns=[*admin_columns, "count"])
                else:
                    selected["geometry"] = selected.geometry.representative_point()
                    joined = gpd.sjoin(selected, admin2, predicate="within", how="inner")
                    grouped = (
                        joined.groupby(admin_columns)
                        .size()
                        .rename("count")
                        .reset_index()
                    )
                write_frame_parquet_atomic(grouped, checkpoint)
                write_checkpoint_manifest(
                    manifest,
                    expected,
                    selected_rows=len(selected),
                    grouped_rows=len(grouped),
                )
                logger.info(
                    "tourism layer processed layer=%s selected=%d grouped=%d",
                    name,
                    len(selected),
                    len(grouped),
                    extra={"action": "aggregate_layer", "domain": "tourism", "phase": name, "path": str(source_path)},
                )
            layer_counts.append(grouped)

        if layer_counts:
            combined_counts = (
                pd.concat(layer_counts, ignore_index=True)
                .groupby(admin_columns, as_index=False)["count"]
                .sum()
                .rename(columns={"count": "tourism_poi_count"})
            )
        else:
            combined_counts = pd.DataFrame(columns=[*admin_columns, "tourism_poi_count"])
        counts = admin2[admin_columns].merge(combined_counts, on=admin_columns, how="left")
        counts["tourism_poi_count"] = counts["tourism_poi_count"].fillna(0).astype(int)
        counts["year"] = int(ctx.config.data["years"]["static_merge"])
        write_frame_csv_atomic(counts, ctx.output("tourism.csv"))
        logger.info("tourism rows=%d", len(counts), extra={"domain": "tourism"})
