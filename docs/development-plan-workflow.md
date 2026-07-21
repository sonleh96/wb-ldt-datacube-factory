# Admin-2 development-plan workflow

`ldt-plans` discovers, reviews, downloads, and publishes the latest formally adopted admin-2 development plans independently of the PIL indicator and scoring pipeline.
It shares repository conventions for YAML configuration, structured logs, resumable state, and administrative identifiers, but it is never invoked by `ldt-factory run`.

## Acceptance policy

The preferred artifact is the latest formally adopted or approved full development plan for the exact admin-2 jurisdiction.
A draft, consultation notice, decision to prepare a plan, budget, spatial plan, annual report, implementation report, or citizen summary is not accepted as the full plan.
An expired plan can be approved manually when it is demonstrably the latest formally adopted plan available.
Automatic acceptance requires a current plan period, verified formal status, an exact jurisdiction match, a full document, and an authoritative government source.
Automatic acquisition is disabled in the committed country configurations until held-out evaluation demonstrates sufficient precision.

## Country contracts

The committed profiles are:

- `config/development_plans/alb.yaml` for 61 Albanian municipalities from `GPBP_LDT_ALB_admin_2.csv`.
- `config/development_plans/srb.yaml` for 161 Serbian municipalities from `GPBP_LDT_SRB_admin_2_v2.csv`.
- `config/development_plans/zmb.yaml` for 116 Zambian districts from `GPBP_LDT_ZMB_admin_2.csv`.

The Albania profile searches Albanian municipal strategic-development terminology and writes under `gs://wb-ldt/ldt/sources_albania/municipalities/{storage_slug}/`.
The Serbia profile searches Serbian Cyrillic and Latin terminology and writes under `gs://wb-ldt/ldt/sources/municipal/{storage_slug}/`.
The Zambia profile searches Integrated Development Plan terminology and writes under `gs://wb-ldt/ldt/sources_zambia/districts/{storage_slug}/`.
Each admin-2 area receives a stable identifier even when the source panel contains one row per year.
Serbia preserves the legacy ASCII replacement slug convention, including folder names such as `ba-ka-palanka`.

Load the ignored repository-local secret file before running a profile:

```powershell
. .\secrets.ps1
```

The Exa key and Google credentials must remain outside YAML and Git.
`GOOGLE_APPLICATION_CREDENTIALS` can point to the same service-account key used by Earth Engine.
The Google identity needs permission to create and update objects in the configured bucket prefixes.

## Workflow

### 1. Validate the contract

```powershell
ldt-plans validate-config `
  --config config/development_plans/srb.yaml
```

Validation checks the YAML contract, registry columns, stable IDs, duplicate panel rows, and storage-slug collisions.

### 2. Run a bounded pilot

```powershell
ldt-plans discover `
  --config config/development_plans/srb.yaml `
  --run-id srb-plans-20260721-pilot `
  --limit 3
```

The first pass performs an exact native-language search.
The second pass performs a broader deep search with country-specific query variations.
The third pass runs only when the first two passes do not produce an unambiguous high-quality result.
Each search response is converted into structured candidate evidence and saved immediately under `runs/{run_id}/candidates/`.
Rerunning the same command and run ID resumes completed area records unless `--force` is supplied.

Use one or more `--admin2-id` options to target known areas during recovery.

### 3. Export the review workbook

```powershell
ldt-plans export-review `
  --config config/development_plans/srb.yaml `
  --run-id srb-plans-20260721-pilot
```

The workbook contains `Summary`, `Review Queue`, `All Candidates`, and `Run Metadata` sheets.
Only the yellow columns in `Review Queue` are intended for editing.
Set `reviewer_decision` to `APPROVE`, `REJECT`, or `MISSING`.
Place a corrected document or official landing-page URL in `replacement_url` when the proposed URL is broken or wrong.
Set `pin_selection` to `TRUE` when subsequent discovery runs must not silently replace the reviewed choice.

### 4. Import review decisions

```powershell
ldt-plans apply-review `
  --config config/development_plans/srb.yaml `
  --run-id srb-plans-20260721-pilot `
  --workbook D:/Work/WB/LDT/plan_workflows/SRB/runs/srb-plans-20260721-pilot/review.xlsx
```

The importer validates admin-2 identifiers, decision values, duplicate rows, and replacement URLs.
It writes normalized decisions to `runs/{run_id}/decisions/reviewed.json`.
The workbook remains a human interface and is not the canonical machine state.

### 5. Acquire approved documents

```powershell
ldt-plans acquire `
  --config config/development_plans/srb.yaml `
  --run-id srb-plans-20260721-pilot
```

Only explicit `APPROVE` decisions are processed while `auto_accept_enabled` is false.
Every selected URL is freshly retrieved through Exa Contents and must still verify the exact jurisdiction, full development-plan type, and formal status.
The direct document is downloaded to temporary staging with configured size bounds and must pass PDF or Word magic-byte validation.
HTML error pages saved with a document extension are rejected.

The object name is immutable and content addressed:

```text
{prefix}/{storage_slug}/development_plan_{start}-{end}_{sha256_prefix}.{extension}
```

A companion provenance manifest is stored next to the document.
An admin-2 `latest.json` pointer identifies the current object and manifest.
New objects use a zero-generation precondition, and pointer replacements use the observed generation to prevent races and accidental clobbering.
The local run report is written before cleanup.
The temporary document is deleted only after GCS size and CRC32C verification plus successful manifest and pointer writes.

### 6. Evaluate a run

Operational metrics do not require labels:

```powershell
ldt-plans evaluate `
  --config config/development_plans/srb.yaml `
  --run-id srb-plans-20260721-pilot
```

For retrieval and selection evaluation, provide a UTF-8 CSV with these required columns:

```text
admin2_id,candidate_url,relevance
```

Relevance labels use `3` for the exact latest formally adopted full plan, `2` for the correct plan with incomplete authority or status evidence, `1` for a related outdated or draft document, and `0` for an incorrect document.
Optional `document_status`, `start_year`, and `end_year` columns enable field exact-match metrics.

```powershell
ldt-plans evaluate `
  --config config/development_plans/srb.yaml `
  --run-id srb-plans-20260721-pilot `
  --gold D:/Work/WB/LDT/plan_workflows/SRB/evals/gold.csv
```

The report includes NDCG@5, Recall@10, latest-approved Precision@1, auto-accept precision, field exact match, status coverage, pass errors, verified uploads, and recorded Exa cost.
Keep automatic acquisition disabled until a sufficiently large held-out set reaches at least 98 percent auto-accept precision with no wrong-jurisdiction or wrong-document-type acceptances.

## Run artifacts

Each run is isolated under `plan_workflows/{ISO3}/runs/{run_id}/`:

```text
candidates/       resumable per-area search evidence
decisions/        normalized reviewer decisions
downloads/        temporary acquisition staging
logs/             structured discovery and acquisition JSONL logs
reports/          evaluation and verified-upload reports
review.xlsx       human review interface
```

Failed downloads remain available for diagnosis and retry.
Successfully published documents are removed from local staging.
Candidate evidence, reviewer decisions, logs, and reports are retained for reproducibility.
