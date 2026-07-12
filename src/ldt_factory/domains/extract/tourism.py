from __future__ import annotations

import logging

from ...context import RunContext
from ...geo import osm_layer
from ...io_utils import require_files
from ...logging_utils import logged_action


def run(ctx: RunContext, logger: logging.Logger) -> None:
    paths = [
        osm_layer(ctx.config, name)
        for name in (
            "gis_osm_buildings_a_free_1.shp",
            "gis_osm_pois_a_free_1.shp",
            "gis_osm_landuse_a_free_1.shp",
            "gis_osm_transport_a_free_1.shp",
        )
    ]
    with logged_action(logger, "validate_input", domain="tourism"):
        require_files(paths, "OSM tourism layers")
