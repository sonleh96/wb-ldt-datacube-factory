# WB LDT Datacube Factory

Config-driven, restartable extraction and processing for the World Bank LDT
admin-2 datacube. This project turns the exploratory
`LDT/notebooks/datacube.ipynb` workflow into independent domain modules that
can be run through one resource-aware pipeline or as separate processes.

Country boundaries, administrative field names, years, source locations, and
resource limits are declared in YAML.
Credentials remain in external credential stores or environment variables and are never stored in country configuration.

## What the factory provides

- Manually configured admin-0, admin-1, and admin-2 boundary files.
- Ordered web, Google Drive acquisition, and prerequisite stages.
- Separate extraction and processing modules for each indicator domain.
- Dependency-aware parallel execution with CPU, memory, disk, Earth Engine,
  and API concurrency limits.
- Atomic downloads, outputs, checkpoints, task manifests, and run summaries.
- Safe restart through input, configuration, code, upstream, and output
  fingerprints.
- Structured JSONL logs for every task and action.
- Final indicator and score tables keyed by `(admin1, admin2, year)`.
- A release-blocking post-processing quality gate with reusable EDA artifacts.

## Data sources and domains

| Component | Source | Factory behavior |
|---|---|---|
| OSM | Geofabrik | Downloads and extracts the configured country archive. |
| Climate TRACE | Country CO2e 100-year and CH4 packages | Downloads packages, then streams compatible source-emissions CSVs. |
| Population | WorldPop | Downloads yearly rasters and performs blockwise admin-2 aggregation. |
| Key Assets | OSM buildings | Filters schools, universities, and hospitals into GeoParquet and GeoJSON. |
| Transport | OSM roads and railways | Assigns lines to admin-2 boundaries, clips crossings, and calculates lengths. |
| Flood | WRI Aqueduct in Earth Engine | Exports the current hazard specification and calculates network exposure. |
| Land Cover | Google Dynamic World in Earth Engine | Uses either yearly local class rasters or server-side admin-2 reductions, then calculates class-share change and crop area. |
| Luminosity | VIIRS monthly composites in Earth Engine | Exports one annual composite per indicator year. |
| Air Pollution | OpenWeatherMap history API | Acquires resumable grid/year summaries and aggregates PM2.5, PM10, and NO2. |
| Emissions | Climate TRACE | Aggregates CO2e and CH4 by admin-2 and configured indicator year. |
| Heatwaves | GFDL climate-projection NetCDF files in Google Drive or a local glob | Synchronizes and verifies the complete projection set, clips it to the country, and calculates transport exposure to qualifying events. |
| Internet | Global Ookla Parquet files in Google Drive or a local directory | Synchronizes and verifies fixed and mobile files, then filters tiles locally without copying the global data per country. |
| Tourism | OSM POIs, buildings, land use, and transport | Counts configured tourism-related features by admin-2. |
| Accessibility | Mapbox Isochrone API and WorldPop | Required cached extraction and population-weighted school/hospital accessibility. |

## Pipeline order

```text
normalized boundaries
        |
        v
web sources: OSM + Climate TRACE + WorldPop
        |
        v
shared sources: Heatwaves + Ookla from Google Drive
        |
        v
prerequisites: Key Assets + Transport + Population
        |
        v
domain DAG: each extraction unlocks its own processing task
        |
        v
combine indicators and scores
        |
        v
publication EDA and quality gate
```

Flood and Heatwaves depend on Transport. Internet depends on Key Assets.
Accessibility depends on Key Assets and Population. Population and Transport
also feed final publication.

Concurrency is configured independently for each country. `pipeline.max_parallel`
caps worker processes, while `pipeline.resource_limits` controls concurrent
heavy-memory, disk-heavy, Earth Engine, Google Drive download, and rate-limited API tasks.

## Requirements

- Python 3.11 or newer.
- A GDAL/GEOS/PROJ-compatible geospatial Python environment.
- An Earth Engine service account and JSON key for Flood, Land Cover, and
  Luminosity extraction.
- An Earth Engine Cloud project and an uploaded admin-2 table asset when using
  the optional Land Cover `gee_reduce_regions` backend.
- An OpenWeatherMap API key for Air Pollution.
- A Mapbox token for required Accessibility extraction.
- Google Application Default Credentials with Drive read-only scope when a source uses `provider: google_drive`.
- About 21 GiB of shared cache space for the configured Heatwave and Ookla folders, plus working space for country outputs.

The supported Conda environment name is `ldt-factory` on Windows, Linux, and macOS.

## Installation

Run this sequence from the repository root on Windows PowerShell, Linux, or macOS:

```text
conda env create --file environment.yml
conda activate ldt-factory
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
ldt-factory --help
python -m pytest
```

`environment.yml` creates the consistently named Python 3.11 environment.
It installs ABI-sensitive scientific and geospatial packages from conda-forge so NumPy, PyArrow, Numba, GDAL, GEOS, PROJ, and NetCDF-backed libraries come from one compatible native solve.
`requirements.txt` is the required development and test installation entrypoint.
It installs the repository in editable mode with the `geo` and `dev` extras declared in `pyproject.toml`, so the command-line entrypoint and test dependencies are included.

For a reproducible installation from the checked-in universal lock, install the pinned lock tool and synchronize the active Conda environment:

```text
python -m pip install "uv==0.11.26"
uv sync --active --locked --all-extras
```

Use `requirements.txt` for normal development and dependency updates.
Use `uv.lock` for repeatable CI or deployment builds.
Refresh the lock intentionally with `uv lock --upgrade`, rerun the complete test suite, and review all version changes before committing it.

Update an existing environment with:

```text
conda env update --name ldt-factory --file environment.yml --prune
conda activate ldt-factory
python -m pip install --upgrade -r requirements.txt
```

For a non-editable runtime-only installation, use `python -m pip install ".[geo]"` from the repository root.
The remaining portability work is tracked in [the cross-platform deployment plan](docs/cross-platform-deployment-plan.md).

## Country configuration

Country files live under `config/countries/`.
Each YAML file is a complete country definition and can be used as the starting point for another country.

Machine-specific storage locations must stay outside committed country files.
Set one data root using the following precedence:

1. Pass `--data-root` to an `ldt-factory` command.
2. Set the `LDT_DATA_ROOT` environment variable.
3. Set an optional top-level `data_root` in a local or deployment-specific country YAML.

Existing absolute paths remain supported and do not require a data root.
Relative `workspace`, source `cache_dir`, source `dataset_root`, and local `source_glob` values resolve from the data root.
Relative boundary paths resolve from the country workspace.

For the committed Romania configuration, set the root that contains `countries/` and `shared_sources/`:

```powershell
$env:LDT_DATA_ROOT = "D:/Work/WB/LDT"
ldt-factory validate --config config/countries/rou.yaml
```

```bash
export LDT_DATA_ROOT="/srv/ldt"
ldt-factory validate --config config/countries/rou.yaml
```

The equivalent one-command override is:

```text
ldt-factory validate --config config/countries/rou.yaml --data-root /srv/ldt
```

`validate` prints the resolved data root and workspace so operators can confirm path selection before execution.
An unresolved environment variable or a relative path without a data root is a configuration error.

Before using a configuration for another country, update at least:

- `country.iso3`, `country.name`, and the relative `workspace`.
- The three boundary paths.
- `admin1_source_field` and `admin2_source_field` as they appear in the admin-2
  boundary file.
- Output administrative names used in CSV join keys.
- Observation, indicator, baseline, and static-merge years.
- OSM, Climate TRACE, WorldPop, Heatwave, and Ookla source locations or Drive folder IDs.
- Domain and resource settings appropriate for the machine.

`years.static_merge` is the dataframe join year for static snapshots. It is not
the Flood scenario year or Transport source year. The latest Transport and
Accessibility values are broadcast across every indicator year during
publication.

Do not put credential values in the country YAML.
Source sections contain only environment-variable names or non-secret source identifiers such as public Drive folder IDs.

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

On Linux or macOS, export the equivalent values from a secret manager or an untracked shell file:

```bash
export EE_SERVICE_ACCOUNT="service-account@example.iam.gserviceaccount.com"
export EE_KEY_FILE="/secure/location/earth-engine-key.json"
export OWM_API_KEY="..."
export MAPBOX_ACCESS_TOKEN="..."
```

Drive-backed sources use Google Application Default Credentials.
The preferred local setup requests only the `drive.readonly` scope and requires a Desktop OAuth client downloaded from Google Cloud Console.
Set the real downloaded path and validate it before starting authentication:

```powershell
$oauthClient = "C:/path/to/downloaded-desktop-oauth-client.json"
if (-not (Test-Path -LiteralPath $oauthClient)) {
  throw "Desktop OAuth client file not found: $oauthClient"
}
gcloud auth application-default login `
  --client-id-file=$oauthClient `
  --scopes="https://www.googleapis.com/auth/cloud-platform,https://www.googleapis.com/auth/drive.readonly"
```

When a Desktop OAuth client is unavailable, the Cloud SDK provides this alternative:

```powershell
gcloud auth login --enable-gdrive-access --update-adc
```

That alternative grants the Cloud SDK full Google Drive access rather than read-only access.
Use it only when the broader permission is acceptable.

For unattended execution, attach or federate a service account and share both source folders with that service-account address.
Set `GOOGLE_APPLICATION_CREDENTIALS` only when a service-account JSON key is the chosen fallback.
OAuth client files, ADC refresh tokens, and service-account keys must remain outside this repository.

## Land Cover extraction backends

Land Cover defaults to the existing `raster_download` backend.
It downloads one Dynamic World classification raster for each configured year and performs zonal counts locally.

```yaml
sources:
  land_cover:
    backend: raster_download
    pixel_size_m: 10
```

The `gee_reduce_regions` alternative performs the nine Dynamic World class counts on Earth Engine and downloads only the resulting annual admin-2 tables.
Before enabling it, upload the exact normalized local admin-2 boundary to the Earth Engine Assets interface as a table.
The asset properties must use the configured `admin1_output_name` and `admin2_output_name` fields because extraction validates every returned key against the local boundary file.
The configured service account must be able to read that boundary asset and create table assets in the configured Cloud project.

```yaml
sources:
  earth_engine:
    service_account_env: EE_SERVICE_ACCOUNT
    key_file_env: EE_KEY_FILE
    project_id: your-earth-engine-cloud-project
    admin2_asset_id: projects/your-earth-engine-cloud-project/assets/rou_admin2

  land_cover:
    backend: gee_reduce_regions
    pixel_size_m: 10
    tile_scale: 4
    max_pixels_per_region: 1000000000
    poll_seconds: 20
    cleanup_intermediate_assets: false
```

Each year is submitted as a batch table export to a deterministic intermediate Earth Engine asset.
The task ID and status are recorded in `<workspace>/state/land_cover/gee_export_{year}.json`, allowing an interrupted run to resume polling or collect an already completed asset.
The validated local table is saved as `<workspace>/raw_data/land_cover/{ISO3}_{year}_counts.csv` and the normal processor still writes `datasets/lulc.csv`.
Set `cleanup_intermediate_assets: true` to delete each generated intermediate asset after its local table is safely written.
Cleanup failures are logged as warnings and do not discard the local result.
The factory does not silently switch back to large raster downloads when a GEE task fails.

To run only Land Cover after enabling the backend:

```powershell
ldt-factory preflight --config config/countries/<iso3>.yaml
ldt-factory run-domain `
  --config config/countries/<iso3>.yaml `
  --name land_cover `
  --phase all `
  --run-id land-cover-gee
```

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
- `preflight` checks packages, boundaries, credential presence, Drive folder inventories, verified source caches, cache capacity, free disk space, existing Land Cover backend artifacts, and the Air Pollution request estimate.
- It always checks the Mapbox credential required by Accessibility.

`run` does not invoke preflight automatically. Resolve preflight errors before
starting; warnings are reported for operator review but do not block execution.

To synchronize and verify only the two shared Drive sources before domain testing:

```powershell
ldt-factory sync-source --config $config --name heatwaves --run-id heatwaves-source
ldt-factory sync-source --config $config --name internet --run-id internet-source
```

Interrupted transfers retain `.part` files and resume with HTTP Range requests.
Each cache receives an atomic `drive_inventory.json` containing Drive IDs, sizes, checksums, modified times, and verified local paths.

## Run the full pipeline

```powershell
$config = "config/countries/<iso3>.yaml"
$runId = "<iso3>-production-001"
ldt-factory run `
  --config $config `
  --run-id $runId `
  --resume
```

Accessibility is part of every full run and every publication output.
`--include-optional` remains available for any additional domains that a future
country configuration explicitly lists under `pipeline.optional_domains`.

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
ldt-factory run-domain --config $config --name accessibility --phase all --resume

# Publication
ldt-factory combine --config $config --resume

# Post-processing EDA and release quality gate
ldt-factory quality --config $config --resume
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
| `{ISO3}_accessibility.csv` | Required school/hospital accessibility. |

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

After publication, the quality workflow writes country-agnostic evidence under
`<workspace>/quality/`:

| Output | Contents |
|---|---|
| `report.html` | Human-readable QA summary, findings, profiles, and charts. |
| `summary.json` | Machine-readable pass/warn/fail result and artifact inventory. |
| `findings.csv` | Severity, blocking status, counts, rates, impact, and remediation. |
| `indicator_profile.csv`, `score_profile.csv` | Completeness, zero rates, cardinality, and robust numeric summaries. |
| `indicator_by_year.csv`, `score_by_year.csv` | Per-year completeness and distribution summaries. |
| `indicator_outliers.csv` | Review samples outside the configured robust IQR fences. |
| `composite_checks.csv` | Recalculation checks for Infrastructure, Livability, and Prosperity scores. |
| `score_indicator_alignment.csv` | Rank-direction checks between each score and its source indicator. |

The command exits unsuccessfully only for release-blocking defects: invalid or
duplicate keys, incomplete panel grain, mismatched indicator/score keys,
non-numeric or non-finite published values, scores outside 0-100, invalid
configured indicator ranges, inconsistent derived/composite values, or reversed
score direction. Zero dominance, temporal sparsity, and robust outliers remain
visible review findings because they can be legitimate for sparse or static
sources. Publication replaces missing indicator values with zero before score
construction, then zero-fills any remaining missing score values before
writing the final CSVs.

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
- The raster Land Cover backend validates its class range and uses nodata `255`,
  because Dynamic World class `0` is water.
- The server-side Land Cover backend uses an unweighted `reduceRegions` sum for
  all nine classes, validates exact admin-2 key coverage, and passes the compact
  count tables to the same indicator calculation as the raster backend.
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
- `years.static_merge` is the join key for snapshot domain outputs. Transport
  and Accessibility use their latest available values across every indicator
  year.
- Flood currently uses the factory's code-defined WRI Aqueduct scenario. Review
  the Flood extraction module before changing climate scenario, return period,
  model, or hazard year.
- Heatwaves are external NetCDF inputs, not an Earth Engine source.
  Drive-backed configuration requires the exact nine files covering 2015 through 2100.
- Ookla remains a shared global input rather than a per-country copy.
  Drive-backed configuration requires fixed and mobile Parquet files for every configured indicator year.
- Air Pollution request volume depends on boundary extent, grid resolution,
  indicator years, and the configured rate. Preflight reports the request count
  and theoretical minimum duration before extraction begins.
- Preflight reports empty or invalid admin-2 geometries but does not repair them
  automatically because geometry repair can change boundary semantics.
- Existing Land Cover rasters using nodata `0` are invalid because Dynamic World
  class `0` is water. They must be re-extracted with nodata `255` or replaced by
  the `gee_reduce_regions` count-table backend.
- Scores rank across the complete panel by default. Setting
  `scoring.rank_within_year: true` is a methodology change.
- Flood, Heatwaves, and Tourism are snapshots joined to `years.static_merge`.
  Transport and Accessibility are latest-value indicators repeated across all
  panel years.
- Publication excludes the internal fields `transport_source_year`,
  `flood_scenario_year`, `flood_return_period_years`, and `key_structures` from
  all three final output files.

## Testing

Install and test through the same dependency path used by clean deployments:

```text
conda activate ldt-factory
python -m pip install -r requirements.txt
python -m pytest -q
```

Tests cover configuration, contracts, orchestration state, resource limits,
API caches, downloads, Flood batching, Internet filters, Heatwave processing,
Land Cover calculations, Population aggregation, Transport geometry handling,
and publication validation.

GitHub Actions repeats the documented requirements installation and test suite on Ubuntu x64, Windows x64, macOS Intel, and macOS ARM64.
It also verifies that `uv.lock` is current and can create a complete locked environment.

## Repository layout

```text
config/countries/             country YAML files
docs/notebook-audit.md        notebook contradictions and methodology decisions
docs/cross-platform-deployment-plan.md
                              deployment phases and acceptance criteria
environment.yml               ldt-factory Conda environment definition
src/ldt_factory/              CLI, orchestration, state, I/O, and shared helpers
src/ldt_factory/domains/      per-domain extract and process modules
tests/                        regression and contract tests
uv.lock                       universal dependency lock
```

See [docs/notebook-audit.md](docs/notebook-audit.md) for notebook contradictions,
methodology decisions, and intentional source limitations.
