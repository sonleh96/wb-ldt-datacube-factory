from __future__ import annotations

import logging
from pathlib import Path

from .checkpoint_utils import write_frame_csv_atomic
from .context import RunContext
from .geo import load_admin2
from .io_utils import require_files
from .logging_utils import logged_action

KEY_RENAMES = {
    "population_total": "Population",
    "luminosity": "Nighttime Luminosity",
    "railway_length_flood_risk": "Railway Flood Risk (km)",
    "road_length_flood_risk": "Road Flood Risk (km)",
    "road_length": "Total Road Length (km)",
    "rail_length": "Total Railway Length (km)",
    "railway_length_heatwave_risk": "Railway Heatwave Risk (km)",
    "road_length_heatwave_risk": "Road Heatwave Risk (km)",
    "co2_emissions_quantity": "CO2-Equivalent Emissions (tonnes)",
    "hospital_accessibility": "Accessibility to Hospitals (%)",
    "school_accessibility": "Accessibility to Schools (%)",
    "tourism_poi_count": "Number of Tourism POIs",
    "built_pct_change": "Change in Build Area (%)",
    "tree_pct_change": "Change in Forest Area (%)",
    "agri_land": "Total Land Area for Agricultural Use (km2)",
    "pm25": "Average PM25 Concentration (ug/m3)",
    "avg_d_mbps_broadband": "Average Broadband Internet Download Speed (Mbps)",
    "avg_d_mbps_mobile": "Average Mobile Internet Download Speed (Mbps)",
    "key_structures_without_internet": "Key Structures without Access to Broadband Internet (%)",
}

OUTPUT_SPECS = {
    "land_cover": ("lulc.csv", "panel"),
    "luminosity": ("luminosity.csv", "panel"),
    "population": ("{iso3}_population.csv", "panel"),
    "internet": ("{iso3}_internet.csv", "panel"),
    "flood": ("{iso3}_flood.csv", "static"),
    "heatwaves": ("{iso3}_heatwaves.csv", "static"),
    "transport": ("{iso3}_infra_length.csv", "static"),
    "emissions": ("{iso3}_emissions.csv", "panel"),
    "air_pollution": ("{iso3}_air_pollution.csv", "panel"),
    "tourism": ("tourism.csv", "static"),
    "accessibility": ("{iso3}_accessibility.csv", "static"),
}


def _safe_ratio(numerator, denominator, multiplier=1.0):
    import numpy as np

    return np.where(denominator > 0, multiplier * numerator / denominator, np.nan)


def _validate_keys(
    frame,
    *,
    path: Path,
    keys: list[str],
    expected,
    strict_coverage: bool,
    logger: logging.Logger,
) -> None:
    import pandas as pd

    missing_keys = [key for key in keys if key not in frame.columns]
    if missing_keys:
        raise ValueError(f"{path} is missing join keys {missing_keys}")
    if frame.duplicated(keys).any():
        raise ValueError(f"{path} contains duplicate composite keys")
    actual_keys = frame[keys].drop_duplicates()
    unknown = actual_keys.merge(expected, on=keys, how="left", indicator=True)
    unknown = unknown[unknown["_merge"] == "left_only"]
    if not unknown.empty:
        raise ValueError(
            f"{path} contains {len(unknown)} key(s) outside the configured administrative/year contract"
        )
    missing = expected.merge(actual_keys, on=keys, how="left", indicator=True)
    missing = missing[missing["_merge"] == "left_only"]
    if not missing.empty:
        message = f"{path} is missing {len(missing)} expected administrative/year key(s)"
        if strict_coverage:
            raise ValueError(message)
        logger.warning(
            message,
            extra={"action": "validate_coverage", "domain": "publication", "path": str(path)},
        )


def run(ctx: RunContext, logger: logging.Logger, *, include_accessibility: bool = False) -> tuple[Path, Path]:
    import numpy as np
    import pandas as pd

    cfg = ctx.config
    admin2 = load_admin2(cfg)
    projected = admin2.to_crs("EPSG:6933")
    base_admin = admin2[[cfg.admin1, cfg.admin2]].copy()
    base_admin["Total Land Area (km2)"] = projected.geometry.area / 1_000_000.0
    years = cfg.years("indicators")
    base = base_admin.merge(pd.DataFrame({"year": years}), how="cross")

    configured_domains = list(dict.fromkeys(cfg.pipeline.get("main_domains", [])))
    publication_units = ["population", "transport", *configured_domains]
    if include_accessibility:
        publication_units.append("accessibility")
    publication_units = list(dict.fromkeys(publication_units))
    unsupported = [name for name in publication_units if name not in OUTPUT_SPECS]
    if unsupported:
        raise ValueError(f"No publication contract is defined for domains: {unsupported}")
    files = [
        (
            name,
            cfg.dataset_dir / OUTPUT_SPECS[name][0].format(iso3=cfg.iso3),
            OUTPUT_SPECS[name][1],
        )
        for name in publication_units
    ]
    require_files([path for _, path, _ in files], "domain outputs")
    strict_coverage = bool(cfg.pipeline.get("strict_output_coverage", False))

    with logged_action(logger, "combine", domain="publication"):
        combined = base.copy()
        for name, path, coverage in files:
            frame = pd.read_csv(path)
            keys = [cfg.admin1, cfg.admin2, "year"]
            expected_years = years if coverage == "panel" else [int(cfg.data["years"]["static_merge"])]
            expected = base_admin.merge(pd.DataFrame({"year": expected_years}), how="cross")[keys]
            _validate_keys(
                frame,
                path=path,
                keys=keys,
                expected=expected,
                strict_coverage=strict_coverage,
                logger=logger,
            )
            drop_overlap = [column for column in frame.columns if column in combined.columns and column not in keys]
            if drop_overlap:
                raise ValueError(
                    f"{path} overlaps previously published columns: {sorted(drop_overlap)}"
                )
            combined = combined.merge(frame, on=keys, how="left", validate="one_to_one")
            logger.info(
                "publication input merged domain=%s rows=%d columns=%d",
                name,
                len(frame),
                len(frame.columns),
                extra={"action": "merge_domain", "domain": "publication", "phase": name, "path": str(path)},
            )

        combined = combined.rename(columns=KEY_RENAMES)
        derived_ratios = {
            "Luminosity per Capita": ("Nighttime Luminosity", "Population", 1),
            "Luminosity per Area": ("Nighttime Luminosity", "Total Land Area (km2)", 1),
            "Railway Flood Risk (%)": ("Railway Flood Risk (km)", "Total Railway Length (km)", 100),
            "Road Flood Risk (%)": ("Road Flood Risk (km)", "Total Road Length (km)", 100),
            "Railway Heatwave Risk (%)": ("Railway Heatwave Risk (km)", "Total Railway Length (km)", 100),
            "Road Heatwave Risk (%)": ("Road Heatwave Risk (km)", "Total Road Length (km)", 100),
            "C02 Emissions per Area (tonnes/km2)": ("CO2-Equivalent Emissions (tonnes)", "Total Land Area (km2)", 1),
        }
        for output_name, (numerator, denominator, multiplier) in derived_ratios.items():
            if numerator in combined.columns and denominator in combined.columns:
                combined[output_name] = _safe_ratio(
                    combined[numerator], combined[denominator], multiplier
                )
        combined = combined.replace([np.inf, -np.inf], np.nan).rename(columns={"year": "Year"})

        score = combined[[cfg.admin1, cfg.admin2, "Year"]].copy()
        specifications = {
            "Broadband Internet Score": ("Average Broadband Internet Download Speed (Mbps)", True),
            "Mobile Internet Score": ("Average Mobile Internet Download Speed (Mbps)", True),
            "Key Structure Internet Access Score": ("Key Structures without Access to Broadband Internet (%)", False),
            "Railway Heatwave Score": ("Railway Heatwave Risk (%)", False),
            "Road Heatwave Score": ("Road Heatwave Risk (%)", False),
            "Road Flood Score": ("Road Flood Risk (%)", False),
            "Railway Flood Score": ("Railway Flood Risk (%)", False),
            "Emissions Normalized Score": ("C02 Emissions per Area (tonnes/km2)", False),
            "Air Quality Score": ("Average PM25 Concentration (ug/m3)", False),
            "Deforestation Score": ("Change in Forest Area (%)", False),
            "Emissions Score": ("CO2-Equivalent Emissions (tonnes)", False),
            "Luminosity per Capita Score": ("Luminosity per Capita", True),
            "Luminosity per Area Score": ("Luminosity per Area", True),
            "Built Area Development Score": ("Change in Build Area (%)", True),
            "Tourism Score": ("Number of Tourism POIs", True),
            "Agricultural Land Score": ("Total Land Area for Agricultural Use (km2)", True),
        }
        if include_accessibility:
            specifications.update(
                {
                    "Accessibility to Hospitals Score": ("Accessibility to Hospitals (%)", True),
                    "Accessibility to Schools Score": ("Accessibility to Schools (%)", True),
                }
            )
        specifications = {
            score_name: settings
            for score_name, settings in specifications.items()
            if settings[0] in combined.columns
        }
        rank_within_year = bool(cfg.data.get("scoring", {}).get("rank_within_year", False))
        for score_name, (indicator, ascending) in specifications.items():
            if rank_within_year:
                score[score_name] = 100 * combined.groupby("Year")[indicator].rank(
                    method="average", ascending=ascending, pct=True
                )
            else:
                score[score_name] = 100 * combined[indicator].rank(
                    method="average", ascending=ascending, pct=True
                )

        infrastructure = [name for name in specifications if name in {
            "Broadband Internet Score", "Mobile Internet Score", "Key Structure Internet Access Score",
            "Accessibility to Hospitals Score", "Accessibility to Schools Score", "Railway Heatwave Score",
            "Road Heatwave Score", "Road Flood Score", "Railway Flood Score",
        }]
        livability = [name for name in ("Emissions Score", "Air Quality Score", "Deforestation Score", "Emissions Normalized Score") if name in score]
        prosperity = [name for name in ("Luminosity per Capita Score", "Luminosity per Area Score", "Built Area Development Score", "Tourism Score", "Agricultural Land Score") if name in score]
        if infrastructure:
            score["Infrastructure Score"] = score[infrastructure].mean(axis=1, skipna=True)
        if livability:
            score["Livability Score"] = score[livability].mean(axis=1, skipna=True)
        if prosperity:
            score["Prosperity Score"] = score[prosperity].mean(axis=1, skipna=True)
        numeric = score.select_dtypes(include="number").columns
        score[numeric] = score[numeric].round(2)

        indicators_path = cfg.dataset_dir / f"GPBP_LDT_{cfg.iso3}_admin_2.csv"
        scores_path = cfg.dataset_dir / f"GPBP_LDT_{cfg.iso3}_scores_admin_2.csv"
        write_frame_csv_atomic(combined, indicators_path)
        write_frame_csv_atomic(score, scores_path)
        logger.info("indicator rows=%d score rows=%d", len(combined), len(score), extra={"domain": "publication"})
        return indicators_path, scores_path
