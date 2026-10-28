# Energy storage testing

Supervised energy-storage experiments using a Keysight B2902B source-measure unit.
Use `python -m smu` from the repository root to plan, run, validate, and analyze recordings.
The implemented procedure is preconditioning: charge, rest, discharge, rest, repeated for the configured cycle count.

## Safety and scope

This is experimental supervised bench software, not an unattended charger or an independently qualified protection system.

- Review the actual specimen datasheet, polarity, wiring, sensing, voltage/current ratings, and independent protection before enabling outputs.
- Repository YAML profiles are specimen-specific examples, not universally safe templates.
- A passing plan checks configuration consistency, not physical safety. Validation checks saved evidence, not calibration or electrical isolation.
- VISA and filesystem calls can block. Software deadlines do not guarantee a hard response time or shutdown through a failed connection.
- There is no automatic live retry, resume, or external-power-supply control. Preserve failed and partial recordings.
- Do not launch two single-channel runners against the same instrument. Use the shared-session dual runner.

## Environment

Keep the known-working instrument-PC environment:

```powershell
.\.SMUvenv\Scripts\Activate.ps1
python -m smu --help
```

For a fresh environment only, use Python 3.11 or newer:

```powershell
python -m venv .SMUvenv
.\.SMUvenv\Scripts\Activate.ps1
python -m pip install PyYAML PyVISA pytest pyflakes
```

Live communication also requires a suitable VISA backend; PyVISA alone does not install a vendor VISA library.
These dependencies are not a pinned or independently qualified environment specification.

## Commands

| Command | Purpose |
|---|---|
| `plan --config FILE` | Single-channel configuration preview; no hardware connection |
| `run --config FILE` | Single-channel preview; live execution requires explicit authorization |
| `check DIRECTORY` | Validate single-channel experiment evidence offline |
| `analyze DIRECTORY OUTPUT_DIRECTORY` | Export all active phases of a completed single-channel experiment |
| `analyze-phase DIRECTORY OUTPUT_DIRECTORY` | Export one recorded active phase offline |
| `dual-plan --config FILE1 --config FILE2` | Dual-channel configuration preview; no hardware connection |
| `dual-run --config FILE1 --config FILE2` | Dual-channel preview; live execution requires explicit authorization |
| `dual-check DIRECTORY` | Validate a dual-channel batch offline |
| `dual-simulate --config FILE1 --config FILE2` | Exercise the coordinator with synthetic endpoints and virtual time |
| `dual-probe --config FILE1 --config FILE2` | Profile validation; optional connected outputs-OFF setup check |

Use `python -m smu COMMAND --help` for command-specific options.
Run separate commands on separate lines. PowerShell continuation backticks must not have trailing spaces.

## Review the configuration

Review specimen identity and ratings, VISA resource, channel, sensing, NPLC, sample interval, maximum sample gap, cycle count, charge settings, rests, discharge settings, and recording root.

Under `charge.current`, select exactly one of `current_a`, `current_fraction`, or `c_rate`; omit the others or set them to null.
Absolute current is in amperes. A fraction multiplies the supplied maximum charge-current rating.
C-rate requires an explicit `nominal_capacity_ah` and is supported for battery, EDLC, and LIC categories.
Legacy basis/factor input must not be mixed with the newer fields. Discharge current selection is unchanged.
Inspect the resolved currents in the plan; current-rating and termination-current checks still apply.

Unknown fields and duplicate YAML keys are rejected.
Live-required monitoring includes `charge.cv_voltage_tolerance_v` and `acquisition.maximum_sample_gap_s`.
The maximum gap must be at least the sample interval and less than the termination hold time.
Different profiles may use different specimens, settings, and cycle counts; changing `experiment.type` alone does not implement a new procedure.

## Single-channel workflow

### 1. Preview

```powershell
python -m smu plan --config .\configs\preconditioning.local.yaml
```

Create and review your local profile before using it. Preview does not connect, enable output, or create experiment recordings.
Planning does not query or qualify instrument trace capacity.

### 2. Execute after bench review

Template only: replace every placeholder, including the independently reviewed numeric ceilings.

```powershell
python -m smu run `
    --config .\configs\preconditioning.local.yaml `
    --execute `
    --experiment-id REPLACE_UNIQUE_ID `
    --confirm-specimen REPLACE_EXACT_SPECIMEN_ID `
    --expected-serial REPLACE_VERIFIED_SERIAL `
    --max-voltage-v REPLACE_REVIEWED_VOLTAGE_CEILING `
    --max-current-a REPLACE_REVIEWED_CURRENT_CEILING
```

Without `--execute`, `run` previews only. Specimen confirmation must match the YAML; instrument identity is checked against the expected serial.
Discharge preparation uses the supplied DUT maximum voltage as voltage compliance, which may exceed the charge target.
Keep the reported `experiment_directory`. Do not automatically restart after an error.

### 3. Validate and analyze

Use the actual reported directory and a new analysis destination:

```powershell
$ExperimentDir = ".\runs\REPLACE_REPORTED_EXPERIMENT_DIRECTORY"
python -m smu check "$ExperimentDir"
New-Item -ItemType Directory -Force .\analysis_outputs | Out-Null
python -m smu analyze "$ExperimentDir" .\analysis_outputs\REPLACE_NEW_EXPORT_NAME
```

Whole-experiment analysis requires valid completed evidence. The output parent must exist; the export directory must not already exist.
A completed export contains `summary.csv`, `summary.json`, `ANALYSIS_COMPLETE`, and per-active-phase exports.
An error may leave partial output. Do not treat it as finalized without `ANALYSIS_COMPLETE`.

## Dual-channel workflow

Use two reviewed profiles selecting channels 1 and 2, the same exact VISA resource, and different specimen IDs.
Each channel keeps its own phase progression, rest periods, settings, and cycle count.
The live runner uses one shared instrument session. Normal transitions stop their own channel; runtime faults attempt stop-both cleanup and evidence finalization before session closure.

### 1. Preview

```powershell
python -m smu dual-plan --config .\configs\preconditioning.channel1.yaml --config .\configs\preconditioning.channel2.yaml
```

Review both resolved profiles and specimen identities before authorizing execution.

### 2. Execute a supervised qualification run

Template only: replace the batch ID, verified serial, and exact specimen IDs.
Supply specimen confirmations in channel 1/channel 2 order.

```powershell
python -m smu dual-run `
    --config .\configs\preconditioning.channel1.yaml `
    --config .\configs\preconditioning.channel2.yaml `
    --execute `
    --qualification-run `
    --batch-id REPLACE_UNIQUE_BATCH_ID `
    --expected-serial REPLACE_VERIFIED_SERIAL `
    --confirm-specimen REPLACE_CH1_SPECIMEN_ID `
    --confirm-specimen REPLACE_CH2_SPECIMEN_ID
```

Without `--execute`, `dual-run` previews only. Live execution also requires `--qualification-run`.
Currents and limits come from the reviewed YAML profiles. Placeholder specimen IDs and zero discharge cutoff are refused.
The authorization flag does not establish physical safety or general timing qualification.

Startup requires both outputs OFF, explicitly defines independent trigger groups with `:SYST:GRO:DEF (@1),(@2)`, waits for operation completion, and verifies the group readback before channel setup.
A grouped readback is a startup failure. Before/after grouping evidence is retained by the probe and batch manifest.

### 3. Validate and analyze

Keep the reported `batch_directory` and validate it with the dual checker:

```powershell
$BatchDir = ".\runs\REPLACE_REPORTED_BATCH_DIRECTORY"
python -m smu dual-check "$BatchDir"
```

Use `dual-check`, not the single-channel `check`, for the batch root.
The batch contains `dual_experiment.json` and phase recordings. `DUAL_COMPLETE` is written only after successful cleanup and evidence validation.

There is currently no public whole-batch dual-analysis command. Do not pass the batch root to `smu analyze`.
Individual active-phase recordings can be exported using `analyze-phase`; obtain their directories from the batch manifest rather than guessing folder names.

```powershell
New-Item -ItemType Directory -Force .\analysis_outputs | Out-Null
python -m smu analyze-phase REPLACE_PHASE_RECORDING_DIRECTORY .\analysis_outputs\REPLACE_NEW_PHASE_EXPORT_NAME
```

## Recordings and analysis

- `output.root_directory` controls the recording root; relative paths depend on the working directory.
- Single-channel experiments use `experiment.json`; dual batches use `dual_experiment.json`.
- Active-phase recordings use `metadata.json`, `measurements.csv`, and `COMPLETE` when successfully finalized.
- Recording, experiment/batch, and analysis completion are different states. A marker is not proof against later modification.
- Analyze only finalized, unchanged inputs. Preserve raw evidence and keep derived exports separate. Validation and revalidation are not a transactional filesystem snapshot.
- Signed charge and energy retain measured signs. Directional totals are nonnegative magnitudes, not automatically device-labelled delivered or absorbed quantities.
- Integration covers the first-to-last recorded sample, not unrecorded transitions or rests.
- Compliance warnings need interpretation in the context of source mode and phase; they are not automatically acquisition failures.
- Manifest references can be absolute. Copies may retain original paths; do not silently rewrite evidence or use an unrelated recording to satisfy a reference. Review `--phase-root` deliberately where supported.
- Keep generated recordings and analysis out of source commits. `.gitignore` does not untrack files already committed.

## Developer checks

These commands do not replace electrical qualification:

```powershell
python -m pyflakes smu.py drivers acquisition experiments recording analysis checks tests
python -m pytest tests -q
git diff --check
```

Do not lint the virtual environment or run hardware diagnostics indiscriminately.

`dual-simulate` is offline simulation, not evidence of electrochemistry, live timing, physical shutdown, or recording durability.
For deliberate simulated faults, see `dual-simulate --help`.

`dual-probe` does not connect by default. Adding `--connect --expected-serial SERIAL` opens a session and changes instrument configuration while attempting to keep both outputs OFF; it does not enable output or start acquisition.
Use its connected mode only with both DUTs disconnected and no competing controller. It is not a passive identity query or physical-isolation test.
`session_close_returned` means the driver's close method returned, not independent proof that every underlying resource closed.

## Repository layout

| Path | Responsibility |
|---|---|
| `smu.py` | Public CLI and single-channel experiment-wide analysis |
| `configs/` | Specimen-specific experiment profiles |
| `experiments/` | Planning, monitoring coordination, and execution |
| `drivers/` | Instrument/session/channel and SCPI helpers |
| `acquisition/` | Acquisition and trace transports |
| `recording/` | CSV, metadata, manifests, and finalization |
| `analysis/` | Offline validation, calculations, and phase exports |
| `checks/` | Deliberately invoked hardware diagnostics |
| `tests/` | Offline automated tests |
