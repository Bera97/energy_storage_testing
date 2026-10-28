# Energy storage testing

Supervised preconditioning experiments using a Keysight B2902B source-measure unit.
Run `python -m smu` from the repository root.
The procedure is charge, rest, discharge, rest, repeated for the configured cycle count.

## Safety and scope

This is experimental supervised bench software, not an unattended charger or an independently qualified protection system.
Review the actual specimen datasheet, ratings, polarity, wiring, sensing, instrument operating envelope, and independent protection before executing.
Planning validates supplied settings; it does not verify physical safety. Evidence validation does not establish calibration or electrical isolation.
VISA and filesystem calls can block, so software deadlines do not guarantee a hard shutdown response time.
There is no automatic retry, resume, or external-power-supply control. Preserve failed recordings.

`--execute` is the explicit instruction to run hardware. Without it, run commands preview only.
There are no interactive confirmations and no required specimen-ID confirmation arguments.
Do not run two single-channel controllers against the same instrument; use `dual-run`.

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

Live communication also requires a suitable VISA backend. PyVISA alone does not install a vendor VISA library.
These dependencies are not a pinned or independently qualified environment specification.

## YAML configuration

Set specimen identity, ratings, channel, VISA address, sensing, sampling, cycle count, currents, voltage targets, termination, rests, timeouts, and recording root in YAML.
Repository profiles are specimen-specific examples, not universally safe templates.
Unknown fields and duplicate YAML keys are rejected.

For the short execution commands, add this top-level section to each profile and review all values for your setup:

```yaml
execution:
  expected_serial: MY61391173
  maximum_voltage_v: 5.55
  maximum_current_a: 0.1
```

These are the current bench-example values, not universal limits. The serial is checked against the connected instrument; it is not inferred from the VISA address.
The ceilings must cover the configured phase currents and discharge voltage compliance, which uses the supplied DUT maximum voltage and may exceed the charge target.
They remain operator-supplied limits, not independent verification of specimen ratings or instrument capability.
Both dual profiles must specify the same expected serial. Each profile has its own checked ceilings.
Execution settings are retained in the configuration snapshot; no YAML value automatically authorizes output.

For `charge.current`, set exactly one of `current_a`, `current_fraction`, or `c_rate`; omit the others or set them to null.
C-rate requires `nominal_capacity_ah`. A fraction multiplies the supplied maximum charge-current rating.
Legacy basis/factor input must not be mixed with these selections. Inspect resolved currents in the plan.
Discharge current selection remains `basis: rated_max_discharge_fraction` with its configured factor.

Live monitoring requires `charge.cv_voltage_tolerance_v` and `acquisition.maximum_sample_gap_s`.
The maximum gap must be at least the sample interval and less than the termination hold time.
Changing `experiment.type` alone does not implement a different procedure.

## Single channel

The following uses the channel-1 profile; substitute another reviewed YAML profile when needed.

```powershell
python -m smu plan --config .\configs\preconditioning.channel1.yaml
python -m smu run --config .\configs\preconditioning.channel1.yaml --execute
```

Experiment IDs are generated automatically. An optional `--experiment-id YOUR_UNIQUE_ID` overrides the generated ID; existing destinations are refused.
Keep the reported `experiment_directory`.

```powershell
$ExperimentDir = ".\runs\REPLACE_REPORTED_EXPERIMENT_DIRECTORY"
python -m smu check "$ExperimentDir"
New-Item -ItemType Directory -Force .\analysis_outputs | Out-Null
python -m smu analyze "$ExperimentDir" .\analysis_outputs\REPLACE_NEW_EXPORT_NAME
```

Whole-experiment analysis requires valid completed evidence. Its destination must be new, outside the recordings, and have an existing parent.

## Two channels on one instrument

Use profiles for channels 1 and 2 with the same exact VISA address, different specimen IDs, the same output root, and matching expected serials.
Each channel keeps its own phase progression, settings, rests, and cycle count.

```powershell
python -m smu dual-plan --config .\configs\preconditioning.channel1.yaml --config .\configs\preconditioning.channel2.yaml
python -m smu dual-run --config .\configs\preconditioning.channel1.yaml --config .\configs\preconditioning.channel2.yaml --execute
```

There is no `--qualification-run` option. Batch IDs are generated automatically; use `--batch-id YOUR_UNIQUE_ID` only when a custom ID is needed.
Placeholder specimen IDs and zero discharge cutoff remain refused for live dual execution.
Keep the reported `batch_directory`.

```powershell
$BatchDir = ".\runs\REPLACE_REPORTED_BATCH_DIRECTORY"
python -m smu dual-check "$BatchDir"
```

The runner owns one shared instrument session. Normal transitions stop their own channel; faults attempt stop-both cleanup and failure recording before closure.
Startup requires outputs OFF, explicitly sets independent trigger groups with `:SYST:GRO:DEF (@1),(@2)`, waits for operation completion, and verifies the group readback before channel setup.

Use `dual-check`, not the single-channel `check`, for a batch root.
There is currently no public whole-batch dual-analysis command. Do not pass a dual batch root to `smu analyze`.
Export individual active recordings using directories referenced by the batch manifest:

```powershell
New-Item -ItemType Directory -Force .\analysis_outputs | Out-Null
python -m smu analyze-phase REPLACE_PHASE_RECORDING_DIRECTORY .\analysis_outputs\REPLACE_NEW_PHASE_EXPORT_NAME
```

## Recordings and analysis

- `output.root_directory` controls the recording root; relative paths depend on the working directory.
- Single-channel experiments use `experiment.json`; dual batches use `dual_experiment.json`.
- Active recordings contain `metadata.json`, `measurements.csv`, and `COMPLETE` when successfully finalized.
- `DUAL_COMPLETE` records dual-batch finalization after successful cleanup and evidence validation.
- A finalized whole-single-experiment analysis contains combined summaries, per-phase exports, and `ANALYSIS_COMPLETE`. Individual `analyze-phase` exports contain `analysis.csv` and `summary.json` without that whole-export marker.
- Completion markers describe distinct lifecycle stages, not proof against later modification.
- Analyze only finalized, unchanged inputs. Validation is not a transactional filesystem snapshot.
- Signed quantities retain measured signs. Directional totals are nonnegative magnitudes, not automatically device-labelled delivered or absorbed energy.
- Integration covers the first-to-last recorded sample, not unrecorded transitions or rests.
- Compliance flags need interpretation in the context of source mode and phase; they are not automatically acquisition failures.
- References may be absolute. Copied recordings can retain original paths; do not silently rewrite evidence or guess another phase directory. Review `--phase-root` deliberately where supported.
- Keep raw evidence and generated analysis out of source commits. `.gitignore` does not untrack already committed files.

## Developer commands

```powershell
python -m pyflakes smu.py drivers acquisition experiments recording analysis checks tests
python -m pytest tests -q
git diff --check
```

`dual-simulate` exercises virtual scheduling and synthetic endpoints without hardware. It does not qualify electrochemistry, live timing, physical shutdown, or recording durability.
`dual-probe` validates profiles without connection by default. Connected probing changes instrument configuration while attempting to keep both outputs OFF; use it only with both DUTs disconnected and no competing controller.
The connected probe remains a developer diagnostic with its own options; inspect `python -m smu dual-probe --help`.
Do not run hardware diagnostics indiscriminately or lint the virtual environment.

Legacy execution arguments remain accepted for compatibility. Values that duplicate YAML execution settings must agree with YAML; conflicting values are rejected rather than silently overriding it.
Optional transport/planning overrides remain available through command-specific `--help`.
`session_close_returned` means the driver's close method returned, not independent proof that every underlying resource closed.

## Layout

| Path | Responsibility |
|---|---|
| `smu.py` | Public CLI and single-channel whole-experiment analysis |
| `configs/` | Specimen-specific profiles |
| `experiments/` | Configuration, planning, monitoring coordination, execution |
| `drivers/` | Instrument/session/channel and SCPI helpers |
| `acquisition/` | Acquisition and trace transports |
| `recording/` | CSV, metadata, manifests, finalization |
| `analysis/` | Offline validation, calculation, phase export |
| `checks/` | Deliberately invoked hardware diagnostics |
| `tests/` | Offline automated tests |

## Charging/discharging rate-sweep planning
This increment is offline planning only; there is no live sweep executor.
```powershell
python -m smu sweep-plan --config configs/charging.sweep.yaml
python -m smu sweep-plan --config configs/discharging.sweep.yaml
```
Add `--json` for the full plan and configuration snapshots.
The profile path is relative to the sweep YAML file.
Charging values are C-rates; discharging values are fractions of rated
maximum continuous discharge current. Points run in the listed order,
with the configured repetitions at each point.
Example rests are 900 seconds after both charge and discharge.
Reference charge current, termination criteria, specimen ratings, and
timeouts require review; the examples are not approved test protocols.
Every point uses the existing finite per-phase acquisition budget.
Oversized phases and invalid currents are rejected, not clamped or skipped.
There is no buffer rollover, automatic sample-rate reduction, or
first-cycle state preparation in this planning increment.
