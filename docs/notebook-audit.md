# Notebook audit and modularization decisions

The notebook is useful as a prototype, but cannot safely be executed as a
country factory without correcting hidden state and missing dependencies.

## Ordering constraints

- Flood processing requires clipped road/rail layers from Transport. Population
  is required for publication, but Flood's network-risk lengths do not use a
  population denominator.
- Heatwave processing requires Transport. The notebook also merges Population,
  although its final risk percentages are calculated against total network
  length rather than population.
- Fixed-internet processing requires Key Assets because connectivity is joined
  to asset centroids.
- Accessibility, omitted from the requested domain list but used in the final
  score, requires both Key Assets and Population and calls Mapbox.
- Tourism reads OSM directly. It could start immediately after OSM extraction,
  but keeping it in the main fan-out is compatible with the requested staging.

## Contradictions and defects in the notebook

- The target factory checkout was an empty initial commit, so there was no
  existing implementation to extend.
- Secrets are embedded in cells (OpenWeatherMap, Mapbox, and an Earth Engine
  service-account path). The factory reads only environment-variable names
  from YAML and never stores secret values in configuration.
- The notebook says Romania but contains Serbia comments, an Albania railway
  path, and the `Asia/Ho_Chi_Minh` timezone.
- OSM filenames alternate between a fixed dated snapshot and a different year.
  Configuration now declares one archive and one extracted directory.
- The OSM download is commented out, so Key Assets, Transport, and Tourism have
  no reproducible upstream extraction in the notebook.
- Ookla acquisition is now an explicit shared-source task because the complete global fixed/mobile Parquet dataset for 2021-2025 is available in a public Google Drive folder.
  Internet validates the synchronized manifest and filters those shared files without copying them per country.
- Land-cover aggregation is defined in the updated notebook. The factory follows
  its 2017 baseline, Dynamic World class-share change, and 10 m crop-area logic.
  It corrects `total` from a class-count mean to a sum and avoids appending the
  built-area table twice; neither correction changes the intended outputs.
- Dynamic World class `0` is water, so using `0` as GeoTIFF nodata silently
  removes water from the land-cover denominator. New exports use nodata `255`
  and are validated before processing; the existing Romania 2017 raster must be
  replaced because it declares nodata `0`.
- Heatwave processing loops over every day from 2015 through 2100 and every
  polygon in Python, which is prohibitively slow. It also mixes output column
  names (`rail_length_km` versus `rail_length_heatwave_risk`) and omits
  `axis=1` in a rename call.
- `set_crs(4326)` is repeatedly used where the source CRS should be checked or
  data should be transformed with `to_crs`.
- EPSG:3857 is used for national length calculations. The factory uses the
  equal-area/equidistant-oriented EPSG:6933 convention already used elsewhere
  in the notebook, while noting that a country-specific projected CRS is better.
- Air-pollution parsing derives grid IDs with `path.rindex('//')`, which fails on
  normal Windows paths. Requests also lack status validation and structured
  retry handling. The configured Romania grid produces 70,320 requests for five
  years, so extraction is explicitly paced, retrying, compact, and resumable;
  it remains a roughly 19.53-hour minimum job at 60 requests/minute.
- GEE boundaries switch between GAUL 2015 and a community GAUL 2024 dataset,
  despite manually supplied boundaries being the requested source of truth.
  Raster exports convert the configured admin-0 geometry to Earth Engine, while
  the optional Land Cover `gee_reduce_regions` backend requires an Earth Engine
  table uploaded from the exact configured admin-2 boundary.
- The Flood `year=2025` value is a dataframe join key for the 2021-2025 panel,
  not the flood scenario year. YAML names it `static_merge`; Flood separately
  retains the 2030 scenario year and 100-year return period. Transport similarly
  retains its source snapshot year.
- The final merge starts from Land Cover, silently dropping years absent there,
  fills all missing values with zero, and then ranks those zeros as observations.
  The modular pipeline keeps domain outputs separate; publication must validate
  expected key coverage before scoring.
  The post-processing quality stage also reports missingness, zero dominance,
  static-year zero-imputation patterns, and score-to-indicator rank direction so
  this notebook behavior cannot pass unnoticed in a release review.
- Administrative level 2 names alone are used as merge keys in several cells.
  They are not guaranteed unique within a country. The factory uses the
  `(admin1, admin2, year)` composite key.
- Climate TRACE file discovery can include confidence and ownership tables with
  incompatible schemas. Only source-emissions tables with required columns
  should be accepted. The optimized reader aggregates chunks immediately and
  publishes only configured indicator years; `co2e_100yr` is retained as
  CO2-equivalent rather than described as mass of elemental CO2.
- Heatwave event counting retains the notebook's non-overlapping five-day event
  definition. The current published result only needs `events > 0`, so the
  implementation short-circuits after the first event and dissolves positive
  cells without changing the binary risk mask.
- Final scoring still ranks across the full panel by default. YAML exposes
  `scoring.rank_within_year`, but changing it to `true` is a methodology change,
  not a performance optimization.

## Intentional gaps that require source decisions

- The global Ookla Parquet cache must be accessible to every worker instance.
  The source task synchronizes it once from Google Drive and country runs consume the verified manifest.
- Heatwave source files are external GFDL NetCDFs rather than GEE products.
  The source task synchronizes the exact 2015-2100 projection set from Google Drive, while local-glob mode remains available for other deployments.
- Live GEE, OpenWeatherMap, Mapbox, Climate TRACE, WorldPop, and Geofabrik runs
  remain dependent on credentials, quotas, licenses, and current upstream URLs.
