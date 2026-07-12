# WB LDT Datacube Factory

Config-driven, restartable extraction and processing for the World Bank LDT
admin-2 datacube. This project turns the exploratory
`LDT/notebooks/datacube.ipynb` workflow into independent domain modules that
can be run through one resource-aware pipeline or as separate processes.

Country boundaries, administrative field names, years, source locations, and
resource limits are declared in YAML. Credentials remain in environment
variables and are never stored in country configuration.

## What the factory provides

- Manually configured admin-0, admin-1, and admin-2 boundary files.
- Ordered web acquisition and prerequisite stages.
- Separate extraction and processing modules for each indicator domain.
- Dependency-aware parallel execution with CPU, memory, disk, Earth Engine,
  and API concurrency limits.
- Atomic downloads, outputs, checkpoints, task manifests, and run summaries.
- Safe restart through input, configuration, code, upstream, and output
  fingerprints.
- Structured JSONL logs for every task and action.
- Final indicator and score tables keyed by `(admin1, admin2, year)`.

## Data sources and domains

| Component | Source | Factory behavior |
|---|---|---|
| OSM | Geofabrik | Downloads and extracts the configured country archive. |
| Climate TRACE | Country CO2e 100-year and CH4 packages | Downloads packages, then streams compatible source-emissions CSVs. |
| Population | WorldPop | Downloads yearly rasters and performs blockwise admin-2 aggregation. |
| Key Assets | OSM buildings | Filters schools, universities, and hospitals into GeoParquet and GeoJSON. |
| Transport | OSM roads and railways | Assigns lines to admin-2 boundaries, clips crossings, and calculates lengths. |
| Flood | WRI Aqueduct in Earth Engine | Exports the current hazard specification and calculates network exposure. |
| Land Cover | Google Dynamic World in Earth Engine | Exports yearly class rasters and calculates class-share change and crop area. |
| Luminosity | VIIRS monthly composites in Earth Engine | Exports one annual composite per indicator year. |
| Air Pollution | OpenWeatherMap history API | Acquires resumable grid/year summaries and aggregates PM2.5, PM10, and NO2. |
| Emissions | Climate TRACE | Aggregates CO2e and CH4 by admin-2 and configured indicator year. |
| Heatwaves | Local climate-projection NetCDF files | Clips the full configured projection set and calculates transport exposure to qualifying events. |
| Internet | Existing global Ookla Parquet files | Filters fixed and mobile tiles locally; it does not download Ookla data. |
| Tourism | OSM POIs, buildings, land use, and transport | Counts configured tourism-related features by admin-2. |
| Accessibility | Mapbox Isochrone API and WorldPop | Optional cached extraction and population-weighted school/hospital accessibility. |

## Pipeline order

```text
normalized boundaries
        |
        v
web sources: OSM + Climate TRACE + WorldPop
        |
        v
prerequisites: Key Assets + Transport + Population
        |
        v
domain DAG: each extraction unlocks its own processing task
        |
        v
combine indicators and scores
```

Flood and Heatwaves depend on Transport. Internet depends on Key Assets.
Accessibility depends on Key Assets and Population. Population and Transport
also feed final publication.

Concurrency is configured independently for each country. `pipeline.max_parallel`
caps worker processes, while `pipeline.resource_limits` controls concurrent
heavy-memory, disk-heavy, Earth Engine, and rate-limited API tasks.

## Requirements

- Python 3.11 or newer.
- A GDAL/GEOS/PROJ-compatible geospatial Python environment.
- An Earth Engine service account and JSON key for Flood, Land Cover, and
  Luminosity extraction.
- An OpenWeatherMap API key for Air Pollution.
- A Mapbox token only when optional Accessibility is enabled.
- Local heatwave NetCDF inputs matching `sources.heatwaves.source_glob`.
- Existing global Ookla fixed/mobile Parquet files for every indicator year.

Examples below use Windows PowerShell and the `geospatial` conda environment.

## Installation

```powershell
$repo = "C:/path/to/wb-ldt-datacube-factory"
Set-Location $repo
conda activate geospatial
python -m pip install -e ".[geo,dev]"
ldt-factory --help
```

The `geo` extra installs runtime geospatial dependencies. The `dev` extra adds
pytest.

## Country configuration

Country files live under `config/countries/`. Each YAML file is a complete
country definition and can be used as the starting point for another country.

Before using a configuration for another country, update at least:

- `country.iso3`, `country.name`, and `workspace`.
- The three boundary paths.
- `admin1_source_field` and `admin2_source_field` as they appear in the admin-2
  boundary file.
- Output administrative names used in CSV join keys.
- Observation, indicator, baseline, and static-merge years.
- OSM, Climate TRACE, WorldPop, heatwave, and Ookla source locations.
- Domain and resource settings appropriate for the machine.

`years.static_merge` is the dataframe join year for static snapshots. It is not
the Flood scenario year or Transport source year.

Do not put credential values in the country YAML. Source sections contain only
the names of environment variables that the factory should read.

## Credentials

Create a local `secrets.ps1` in the repository root:

```powershell
$env:EE_SERVICE_ACCOUNT = "service-account@example.iam.gserviceaccount.com"
$env:EE_KEY_FILE = "D:/secure/location/earth-engine-key.json"
$env:OWM_API_KEY = "..."
$env:MAPBOX_ACCESS_TOKEN = "..."  # Required only for Accessibility.
```

The file is ignored by Git. Load it into the current PowerShell session before
running credential-dependent tasks:

```powershell
. .\secrets.ps1
git check-ignore secrets.ps1
```

Preflight reports only whether each credential is present; it does not print
credential values.

## Inspect before running

```powershell
$config = "config/countries/<iso3>.yaml"
ldt-factory validate --config $config
ldt-factory plan --config $config
ldt-factory preflight --config $config
```

- `validate` checks YAML structure and required boundary paths.
- `plan` prints stages, dependencies, and resource assignments without running
  work. Add `--json` for machine-readable output.
- `preflight` checks packages, boundaries, credential presence, local Heatwave
  and Ookla inputs, free disk space, existing Land Cover rasters, and the Air
  Pollution request estimate. Add `--include-optional` to check Accessibility.

`run` does not invoke preflight automatically. Resolve preflight errors before
starting; warnings are reported for operator review but do not block execution.

## Run the full pipeline

```powershell
$config = "config/countries/<iso3>.yaml"
$runId = "<iso3>-production-001"
ldt-factory run `
  --config $config `
  --run-id $runId `
  --resume
```

Add `--include-optional` to execute Accessibility and include it in publication:

```powershell
$config = "config/countries/<iso3>.yaml"
$runId = "<iso3>-production-001-accessibility"
ldt-factory run `
  --config $config `
  --run-id $runId `
  --resume `
  --include-optional
```

A full run holds a workspace lock so a second orchestrator cannot overwrite the
same canonical outputs. The scheduler still launches eligible tasks in parallel
inside that run.

Monitor the run from another PowerShell window:

```powershell
$config = "config/countries/<iso3>.yaml"
$runId = "<iso3>-production-001"
$workspace = "C:/path/to/country/workspace"

ldt-factory status `
  --config $config `
  --run-id $runId

Get-Content "$workspace/logs/$runId/orchestrator.jsonl" -Wait
```

After an interruption, rerun the same command and run ID with `--resume`.
Completed tasks and domain checkpoints that still validate will be reused.

## Run individual stages

Individual commands are useful for testing, recovery, or intentionally running
different domains in separate terminals. Multiple machines may also run
different tasks when they can access the same configured inputs and workspace.

```powershell
$config = "config/countries/<iso3>.yaml"

# Web acquisition
ldt-factory run-web --config $config --source osm --resume
ldt-factory run-web --config $config --source climate_trace --resume
ldt-factory run-web --config $config --source population --resume

# Prerequisites
ldt-factory run-prerequisite --config $config --name key_assets --resume
ldt-factory run-prerequisite --config $config --name transport --resume
ldt-factory run-prerequisite --config $config --name population --resume

# Normalized publication boundary
ldt-factory prepare-boundaries --config $config --resume

# One domain
ldt-factory run-domain --config $config --name flood --phase extract --resume
ldt-factory run-domain --config $config --name flood --phase process --resume
ldt-factory run-domain --config $config --name flood --phase all --resume

# Publication
ldt-factory combine --config $config --resume
ldt-factory combine --config $config --include-accessibility --resume
```

Supported domain names are:

```text
flood, land_cover, luminosity, air_pollution, emissions,
heatwaves, internet, tourism, accessibility
```

Respect the stage dependencies when launching units manually. In particular,
finish the web stage before prerequisites, and finish Key Assets, Transport,
and Population before their dependent processing modules. Do not launch two
instances of the same task against the same country workspace. Direct commands
do not launch missing prerequisites automatically; `--phase process` assumes
its extraction and prerequisite artifacts already exist.

## Resume, force, and status

Task state is stored under `<workspace>/state/`. With `--resume`, a completed
task is skipped only when all of the following still match:

- Country configuration.
- Package source code.
- Direct source inputs.
- Upstream task manifests.
- Recorded output paths, sizes, and modification times.

```powershell
$config = "config/countries/<iso3>.yaml"

# Show the latest orchestrated run.
ldt-factory status --config $config

# Show a particular run as JSON.
ldt-factory status `
  --config $config `
  --run-id "<iso3>-production-001" `
  --json

# Rerun a task wrapper even when its manifest is current.
ldt-factory run-domain `
  --config $config `
  --name flood `
  --phase process `
  --force
```

`--force` bypasses the orchestration manifest. It deliberately does not delete
valid domain-internal checkpoints or downloaded source caches. Remove a
specific checkpoint only after confirming that a clean recomputation is
actually required.

## Workspace layout and outputs

Each country workspace is organized as follows:

```text
<workspace>/
  raw_data/       downloaded and externally supplied source data
  shapefiles/     normalized assets and Transport GeoParquet checkpoints
  datasets/       domain tables and final publication CSVs
  logs/<run-id>/  per-task JSONL logs and orchestrator log
  state/          task manifests, run summaries, and domain checkpoints
```

Domain outputs under `datasets/` are:

| Output | Contents |
|---|---|
| `lulc.csv` | Land Cover indicators. |
| `luminosity.csv` | Annual Nighttime Luminosity. |
| `{ISO3}_population.csv` | Annual population totals. |
| `{ISO3}_internet.csv` | Fixed/mobile speed and key-structure connectivity. |
| `{ISO3}_flood.csv` | Static road and railway Flood exposure. |
| `{ISO3}_heatwaves.csv` | Static road and railway Heatwave exposure. |
| `{ISO3}_infra_length.csv` | Total road and railway lengths. |
| `{ISO3}_emissions.csv` | CO2e and CH4 emissions. |
| `{ISO3}_air_pollution.csv` | PM2.5, PM10, and NO2 concentrations. |
| `tourism.csv` | Tourism feature counts. |
| `{ISO3}_accessibility.csv` | Optional school/hospital accessibility. |

Reusable spatial outputs under `shapefiles/` are:

| Output | Contents |
|---|---|
| `assets.parquet`, `assets.geojson` | Filtered schools, universities, and hospitals. |
| `roads_intersect_{year}.parquet` | Admin-assigned and boundary-clipped road segments. |
| `rails_intersect_{year}.parquet` | Admin-assigned and boundary-clipped railway segments. |

Final publication files are:

```text
GPBP_LDT_{ISO3}_admin_2_regions.geojson
GPBP_LDT_{ISO3}_admin_2.csv
GPBP_LDT_{ISO3}_scores_admin_2.csv
```

Publication rejects duplicate or unknown `(admin1, admin2, year)` keys. Missing
expected keys are warnings by default and can be made fatal with
`pipeline.strict_output_coverage: true`.

## Reliability and performance design

- Downloads retain `.part` files and resume with HTTP Range requests when the
  server supports them.
- ZIP archives are integrity-checked and extracted through an atomic temporary
  directory.
- Flood uses an exact single-raster-cell fast path and checkpointed GeoParquet
  batches while retaining the notebook's mean-depth segment rule.
- Land Cover validates its class range and uses nodata `255`, because Dynamic
  World class `0` is water.
- Luminosity exports one annual raster per indicator year instead of retaining
  every monthly raster.
- Air Pollution stores annual sufficient statistics instead of retaining the
  complete hourly response history in memory.
- Emissions streams required CSV columns and aggregates every chunk
  immediately.
- Heatwaves use bounded Dask chunks, binary-event short-circuiting, and one
  dissolved risk mask.
- Internet filters global Parquet before parsing geometry and caches each
  country/year result.
- Key Assets and Tourism push OSM category filters into OGR.
- Population rasterizes the administrative label grid once and sums aligned
  rasters block by block.
- Transport reads filtered OSM fields, clips only boundary-crossing lines, and
  stores row-grouped GeoParquet checkpoints with input fingerprints.

## Methodology and country-specific checks

- Indicator years, the Land Cover baseline, Transport observation year, and
  static publication year are defined independently in each country YAML.
- `years.static_merge` is only the join key for static domain outputs.
- Flood currently uses the factory's code-defined WRI Aqueduct scenario. Review
  the Flood extraction module before changing climate scenario, return period,
  model, or hazard year.
- Heatwaves are external NetCDF inputs, not an Earth Engine source. Every country
  configuration must resolve the complete intended projection set.
- Ookla files are existing shared inputs. The factory does not download them,
  and preflight verifies the files required for configured indicator years.
- Air Pollution request volume depends on boundary extent, grid resolution,
  indicator years, and the configured rate. Preflight reports the request count
  and theoretical minimum duration before extraction begins.
- Preflight reports empty or invalid admin-2 geometries but does not repair them
  automatically because geometry repair can change boundary semantics.
- Existing Land Cover rasters using nodata `0` are invalid because Dynamic World
  class `0` is water. They must be re-extracted with nodata `255`.
- Scores rank across the complete panel by default. Setting
  `scoring.rank_within_year: true` is a methodology change.
- Flood, Heatwaves, Transport, Tourism, and optional Accessibility are static
  snapshots joined only to `years.static_merge`; they are not annual time
  series merely because the publication table contains multiple panel years.

## Testing

```powershell
conda activate geospatial
python -m pytest
```

Tests cover configuration, contracts, orchestration state, resource limits,
API caches, downloads, Flood batching, Internet filters, Heatwave processing,
Land Cover calculations, Population aggregation, Transport geometry handling,
and publication validation.

## Repository layout

```text
config/countries/             country YAML files
docs/notebook-audit.md        notebook contradictions and methodology decisions
src/ldt_factory/              CLI, orchestration, state, I/O, and shared helpers
src/ldt_factory/domains/      per-domain extract and process modules
tests/                        regression and contract tests
```

See [docs/notebook-audit.md](docs/notebook-audit.md) for notebook contradictions,
methodology decisions, and intentional source limitations.
