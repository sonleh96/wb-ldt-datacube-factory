from __future__ import annotations

from ...context import RunContext
from ...geo import load_admin0


def initialize(ctx: RunContext):
    import ee

    source = ctx.config.source("earth_engine")
    service_account = ctx.require_env(str(source["service_account_env"]))
    key_file = ctx.require_env(str(source["key_file_env"]))
    credentials = ee.ServiceAccountCredentials(service_account, key_file)
    project_id = source.get("project_id")
    options = {"project": str(project_id)} if project_id else {}
    ee.Initialize(credentials=credentials, **options)
    return ee


def configured_region(ctx: RunContext, ee):
    admin0 = load_admin0(ctx.config)
    geometry = admin0.to_crs("EPSG:4326").geometry.union_all()
    return ee.Geometry(geometry.__geo_interface__)
