# Cross-Platform Deployment Plan

## Objective

Make the factory installable, configurable, testable, and operable from the same repository checkout on Windows, Linux, and macOS.
The supported baseline will be Python 3.11 in a Conda environment named `ldt-factory`.
Ubuntu Linux will be the reference unattended deployment platform, while Windows and macOS remain first-class local development and execution platforms.

## Current Blockers

1. The Romania configuration embeds `D:/...` paths for the workspace, boundaries, and shared caches.
2. `FactoryConfig` expands user-home paths but does not expand environment variables or define a portable base directory.
3. Most operator examples use PowerShell variables, continuation syntax, and credential files.
4. The repository has no checked-in Conda environment definition, platform lock files, container image, or CI operating-system matrix.
5. Process-pool behavior uses each operating system's implicit multiprocessing default, which exposes native geospatial libraries to different fork and spawn behavior.
6. There is no automated installation smoke test proving that `requirements.txt` installs the package and exposes the `ldt-factory` command.

## Phase 0: Dependency And Documentation Contract

Status: implemented in this change and pending clean-machine verification in the Phase 4 CI matrix.

- Add `environment.yml` with the environment name `ldt-factory` and Python 3.11.
- Make `requirements.txt` install the local project with the `geo` and `dev` extras from `pyproject.toml`.
- Use `pyproject.toml` as the single dependency-definition source to prevent drift between package metadata and `requirements.txt`.
- Document Conda setup, requirements installation, CLI verification, and tests as one mandatory sequence.
- Provide both PowerShell and POSIX-shell credential examples.

Acceptance criteria:

- `conda env create --file environment.yml` creates an environment named `ldt-factory`.
- `python -m pip install -r requirements.txt` installs all runtime and test dependencies plus the editable repository package.
- `ldt-factory --help` and `python -m pytest` run after that installation sequence.

## Phase 1: Portable Configuration Paths

Status: implemented locally and pending qualification in the Phase 4 operating-system matrix.

- Use one central path resolver in `FactoryConfig`.
- Expand `~` and environment variables such as `${LDT_DATA_ROOT}` before constructing a `Path`.
- Resolve relative workspace and shared-source paths from a user-selected data root.
- Resolve relative boundary paths from the country workspace.
- Select the data root using `--data-root`, then `LDT_DATA_ROOT`, then an optional YAML `data_root`.
- Reject unresolved variables with a configuration error that names the exact field.
- Keep the Romania configuration free of drive letters and machine-specific directories.
- Preserve absolute-path compatibility for existing installations.
- Print resolved non-secret roots during configuration validation.

Acceptance criteria:

- The same committed Romania YAML validates when `LDT_DATA_ROOT` points to a Windows path, a Linux path, or a macOS path.
- No committed country configuration contains a drive letter or machine-specific home directory.
- Unit tests cover environment expansion, relative paths, spaces, Unicode, missing variables, and both Windows and POSIX path forms.

## Phase 2: Runtime And Filesystem Semantics

Status: deterministic spawn behavior is implemented; the remaining filesystem and interruption checks are pending CI qualification.

- Use an explicit multiprocessing context with deterministic spawn behavior on all platforms.
- Verify that every worker entrypoint is importable and all submitted arguments are pickle-safe.
- Add tests for interruption, worker failure, and process-pool cleanup under spawn.
- Audit case-sensitive filename assumptions and remove extension or path comparisons that only work on case-insensitive filesystems.
- Keep atomic replacement on one filesystem and produce a clear error when a configured temporary directory crosses filesystem boundaries.
- Extend lock tests for Windows locking and POSIX `flock` behavior.
- Normalize log and manifest paths with `pathlib` and never serialize platform-specific separators as logical identifiers.

Acceptance criteria:

- The orchestration, lock, resume, and interruption suites pass on all three operating systems.
- A task manifest produced on one operating system can be read on another when the shared data root is mounted at a different absolute path.

## Phase 3: Reproducible Environment Resolution

Status: a universal Python dependency lock and a conda-forge native-library environment are implemented; compatibility remains subject to the operating-system matrix.

- Generate and review `uv.lock` from `pyproject.toml` for Python dependencies and cross-platform environment markers.
- Resolve ABI-sensitive scientific and geospatial libraries together through conda-forge when solver differences can change GDAL, GEOS, PROJ, NetCDF, LLVM, or Arrow compatibility.
- Verify in CI that the lock matches `pyproject.toml` and can install all extras.
- Document a controlled lock-refresh procedure rather than accepting ad hoc environment updates.

Acceptance criteria:

- A clean machine can create the environment without manually selecting package versions.
- Import smoke tests cover GDAL-backed, NetCDF-backed, Arrow-backed, Earth Engine, and Google authentication modules.

## Phase 4: CI Platform Matrix

Status: implemented and pending the first hosted GitHub Actions run.

- Run GitHub Actions jobs on Windows x64, Ubuntu x64, macOS Intel, and macOS Apple Silicon.
- Install through `environment.yml` and `requirements.txt`, not through an undocumented CI-only path.
- Run static configuration checks, the full unit suite, CLI help, plan generation, and fixture-based preflight.
- Keep external API tests opt-in and credential-gated.
- Add a Linux container-build job after the reference image exists.

Acceptance criteria:

- Pull requests cannot merge when any supported platform fails installation or tests.
- CI does not require production credentials for its default test suite.

## Phase 5: Linux Reference Deployment

- Add a versioned Dockerfile based on a supported Python or Conda image.
- Run as a non-root user and mount configuration, credentials, shared source cache, country workspace, and logs as explicit volumes.
- Add an entrypoint that runs validation and optional preflight before execution.
- Provide a systemd service example for a Linux VM deployment that does not use containers.
- Document service-account or workload-identity authentication for Google Drive and Earth Engine without embedding keys in the image.
- Define CPU, memory, disk, and shutdown expectations for long-running extraction tasks.

Acceptance criteria:

- The container and systemd deployment both handle `SIGTERM`, retain resumable state, and restart without corrupting outputs.
- No credential or machine-specific path is included in the image or repository.

## Phase 6: End-To-End Platform Qualification

- Create a small, redistributable fixture country with tiny boundaries and source fixtures.
- Exercise boundaries, one web source, one prerequisite, Drive-manifest handoff, one raster domain, one tabular domain, combine, and quality.
- Run the qualification workflow on Windows, Ubuntu, and macOS.
- Record platform-specific performance and filesystem caveats without changing indicator methodology.

Acceptance criteria:

- Each supported platform produces structurally identical output keys and numerically equivalent results within declared tolerances.
- The operator runbook includes installation, credentials, configuration, preflight, execution, monitoring, resume, upgrade, and recovery.

## Recommended Delivery Order

1. Merge the completed Phase 0 and Phase 1 contracts.
2. Implement Phases 2 and 4 together so runtime changes are exercised by the operating-system matrix.
3. Add Phase 3 lock files after the matrix reveals the compatible native package sets.
4. Add the Linux reference deployment and end-to-end qualification last, using the stabilized configuration and environment contracts.
