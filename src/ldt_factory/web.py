from __future__ import annotations

import logging
from pathlib import Path

from .context import RunContext
from .io_utils import download_file, extract_zip
from .logging_utils import logged_action

WEB_SOURCES = ("osm", "climate_trace", "population")


def _download(ctx: RunContext, logger: logging.Logger, url: str, destination: Path) -> Path:
    use_environment_proxy = bool(
        ctx.config.data.get("network", {}).get("use_environment_proxy", True)
    )
    return download_file(
        url,
        destination,
        use_environment_proxy=use_environment_proxy,
        logger=logger,
        overwrite=ctx.force,
    )


def run(ctx: RunContext, source: str, logger: logging.Logger) -> None:
    if source == "osm":
        _osm(ctx, logger)
    elif source == "climate_trace":
        _climate_trace(ctx, logger)
    elif source == "population":
        _population(ctx, logger)
    else:
        raise ValueError(f"Unknown web source: {source}")


def _osm(ctx: RunContext, logger: logging.Logger) -> None:
    cfg = ctx.config.source("osm")
    archive = ctx.raw(str(cfg["archive_name"]))
    target = ctx.raw(str(cfg["extracted_dir"]))
    with logged_action(logger, "download", domain="osm", path=str(archive)):
        _download(ctx, logger, str(cfg["url"]), archive)
    with logged_action(logger, "extract", domain="osm", path=str(target)):
        extract_zip(archive, target)


def _climate_trace(ctx: RunContext, logger: logging.Logger) -> None:
    cfg = ctx.config.source("climate_trace")
    items = (
        ("co2e_100yr_url", f"climate_trace_{ctx.config.iso3}_CO2"),
        ("ch4_url", f"climate_trace_{ctx.config.iso3}_CH4"),
    )
    for key, stem in items:
        archive = ctx.raw(stem + ".zip")
        destination = ctx.raw(stem)
        with logged_action(logger, "download", domain="climate_trace", phase=key, path=str(archive)):
            _download(ctx, logger, str(cfg[key]), archive)
        with logged_action(logger, "extract", domain="climate_trace", phase=key, path=str(destination)):
            extract_zip(archive, destination)


def _population(ctx: RunContext, logger: logging.Logger) -> None:
    template = str(ctx.config.source("worldpop")["url_template"])
    for year in ctx.config.years("population"):
        filename = f"{ctx.config.iso3.lower()}_pop_{year}_CN_100m_R2025A_v1.tif"
        url = template.format(year=year, iso3=ctx.config.iso3, iso3_lower=ctx.config.iso3.lower())
        with logged_action(logger, "download", domain="population", phase=str(year), path=str(ctx.raw("population", filename))):
            _download(ctx, logger, url, ctx.raw("population", filename))
