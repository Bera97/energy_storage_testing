# Energy storage testing

Recorded preconditioning experiments using a Keysight B2902B source-measure unit.

Normal workflow:

1. Review a specimen-specific YAML configuration.
2. Preview the execution plan without hardware access.
3. Explicitly authorize a live experiment.
4. Validate the saved experiment evidence.
5. Analyze the recorded active phases offline.

Run all commands below from the repository root. PowerShell examples use a backtick for line continuation; do not place spaces after it.

## Safety and scope

A successful configuration check or dry run does not establish electrical safety.
Review the actual DUT datasheet, wiring, polarity, sensing arrangement, instrument operating envelope, and independent protection before live execution.

The software does not control an external power supply, automatically resume interrupted experiments, or automatically retry a live run.
Reported shutdown verification is instrument/software evidence, not proof of physical isolation or a substitute for independent protection.

The committed `configs/preconditioning.test_run.yaml` is an existing specimen-specific test profile, not a universally safe template. It includes placeholder descriptive metadata. Do not reuse its ratings or currents for another device without verification.

## Environment

### Existing working environment

Keep the working environment used for your validated runs. Activate it in PowerShell:

```powershell
.\.SMUvenv\Scripts\Activate.ps1
python --version
```

### Fresh environment only

Do not recreate or upgrade an already working environment merely to follow this section.
The code uses `BaseException.add_note`, so use Python 3.11 or newer. A fully pinned, independently verified dependency specification is not supplied by this README.

```powershell
python -m venv .SMUvenv
.\.SMUvenv\Scripts\Activate.ps1
python -m pip install PyYAML PyVISA pytest pyflakes
```

These are the direct runtime/test/lint packages used by the inspected workflow, not a frozen environment specification.
Live instrument communication also requires a suitable VISA backend. Installing PyVISA alone does not install a vendor VISA library. Retain the known-working VISA setup on the measurement PC.

## 1. Review the configuration

Create a specimen-specific copy rather than silently changing a profile used by previous experiments:

```powershell
Copy-Item .\configs\preconditioning.test_run.yaml .\configs\preconditioning.local.yaml
```

Review every section:

| Section | What to verify |
|---|---|
| `experiment` | Experiment type, name, and number of complete cycles |
| `dut` | Specimen identity, actual model/datasheet, category, capacity/capacitance, and ratings |
| `instrument` | VISA resource, channel, two-/four-wire setting, and NPLC |
| `acquisition` | Sample interval and maximum permitted sample gap |
| `charge` | Current-selection basis/factor, voltage target, CV tolerance, termination, and timeouts |
| `rest` | Rest durations after charge and discharge |
| `discharge` | Current-selection factor, cutoff voltage, and timeout |
| `output` | Recording root directory |

Do not add ad-hoc YAML keys: the current loader rejects unknown fields and duplicate keys.

The current schema selects currents through `basis` and `factor`; it does not expose an arbitrary `current_A` field. Review the resolved currents in the preview rather than assuming that every factor is a fraction of the same quantity.

For live charge monitoring, both `charge.cv_voltage_tolerance_v` and `acquisition.maximum_sample_gap_s` must be explicit. The maximum sample gap must be at least the sampling interval and less than the termination hold time.

## 2. Preview without hardware

Use the live entry point in its explicit dry-run mode:

```powershell
python -m experiments.run_live `
    --config .\configs\preconditioning.local.yaml `
    --dry-run
```

This validates live-required settings and prints a JSON preview without connecting to an instrument, enabling output, or creating experiment recordings.

Review the specimen, resource, channel, resolved phase settings, acquisition budget, and required operator ceilings.
The default software acquisition-planning limit is 100000 records; planning does not query or independently validate instrument capacity.

The older planning-only entry point remains available:

```powershell
python -m experiments `
    --config .\configs\preconditioning.local.yaml `
    --dry-run
```

The planning-only entry point can accept configurations missing monitoring fields that the live-entry preview requires. Prefer the live-entry preview when preparing a live run.

## 3. Explicit live execution

Do not execute a live run merely because a preview succeeded.

Inspect the supported arguments:

```powershell
python -m experiments.run_live --help
```

Live execution requires all of:

- `--execute`
- `--experiment-id`: a unique identifier containing 1-80 ASCII letters, digits, underscores, or hyphens
- `--confirm-specimen`: exactly the configured `dut.specimen_id`
- `--expected-serial`: the reviewed B2902B serial number
- `--max-voltage-v`: an explicitly reviewed positive voltage ceiling
- `--max-current-a`: an explicitly reviewed positive current ceiling

Template only: replace every `REPLACE_...` value after physical and configuration review. The numeric placeholders are intentionally not executable values.

```powershell
python -m experiments.run_live `
    --config .\configs\preconditioning.local.yaml `
    --execute `
    --experiment-id REPLACE_UNIQUE_EXPERIMENT_ID `
    --confirm-specimen REPLACE_SPECIMEN_ID `
    --expected-serial REPLACE_VERIFIED_SERIAL `
    --max-voltage-v REPLACE_REVIEWED_VOLTAGE_CEILING `
    --max-current-a REPLACE_REVIEWED_CURRENT_CEILING
```

Do not automatically copy minimum required ceilings from the preview without checking whether the configured experiment itself is appropriate. In particular, discharge preparation uses the supplied DUT maximum voltage as voltage compliance, which can exceed the charge target.

Optional arguments include `--maximum-records`, `--timeout-ms`, and `--visa-library`; see `--help` before changing them.

Keep the reported `experiment_directory`. Live execution reserves its manifest before connecting and attempts final shutdown/session close before successful finalization.
If execution raises, inspect its error report and any recorded experiment directory. Do not assume an interrupted experiment completed, and do not rerun it automatically.

## 4. Validate saved evidence

After recording has stopped, set the path to the experiment directory actually reported by the runner:

```powershell
$ExperimentDir = ".\runs\REPLACE_WITH_REPORTED_EXPERIMENT_DIRECTORY"

python -m analysis.check_preconditioning_experiment "$ExperimentDir"
```

For machine-readable output:

```powershell
python -m analysis.check_preconditioning_experiment "$ExperimentDir" --json
```

Exit codes:

| Code | Meaning |
|---|---|
| 0 | Valid evidence for a completed experiment |
| 1 | Invalid evidence or validation could not complete |
| 2 | Command-line usage error |
| 3 | Valid evidence, but the experiment is not completed |

Validation is read-only. It checks the manifest and referenced active-phase recordings, not physical shutdown, measurement accuracy, file authenticity, or scientific suitability.
The optional `--phase-root` changes the allowed root for phase references; it defaults to the experiment directory's parent. Do not broaden it merely to bypass a validation failure.

## 5. Analyze one recorded active phase

The existing analysis exporter takes a phase recording directory, not the top-level experiment directory. Select the recording referenced by `experiment.json`; do not guess a phase directory name.

The source must no longer be written. The destination must be new, outside the source recording, and have an existing parent directory.

```powershell
$PhaseDir = "REPLACE_WITH_REFERENCED_PHASE_RECORDING_DIRECTORY"

New-Item -ItemType Directory -Force .\analysis_outputs | Out-Null

python -m analysis.export_recorded_analysis `
    "$PhaseDir" `
    .\analysis_outputs\REPLACE_WITH_NEW_ANALYSIS_DIRECTORY
```

Each successful export writes:

- `analysis.csv`: per-sample time, power, cumulative charge, and cumulative energy fields
- `summary.json`: summary calculations, acquisition outcome, and warnings

Existing export directories are not overwritten. An export interrupted after directory creation can leave partial files; do not treat that directory as a completed export.

Positive and negative integrated totals are nonnegative directional magnitudes, not automatically device-labelled delivered/absorbed quantities. Preserve warnings and acquisition outcomes when interpreting results.

A successful export is not proof that the underlying experiment completed successfully.

### Experiment-wide analysis

A single command to analyze all manifest-referenced phases and generate a combined table is planned, but is not implemented by this README. Until it is added and tested, export each active phase with the command above.

## Recording locations

`output.root_directory` selects the experiment output root. Relative paths are interpreted from the working directory, which should be the repository root.

The experiment directory contains `experiment.json` and referenced phase recordings. Active-phase recordings use files such as `metadata.json`, `measurements.csv`, and, for successfully finalized recordings, a `COMPLETE` marker.

Use the experiment manifest to distinguish experiment completion from individual recording finalization. Preserve failed and partial recordings as evidence; do not mark them successful manually.

Treat raw recordings as source evidence and analysis outputs as derived artifacts. Review Git tracking deliberately; `.gitignore` does not untrack files already committed.

## Offline development checks

```powershell
python -m pyflakes `
    drivers `
    acquisition `
    experiments `
    recording `
    analysis `
    commissioning `
    diagnostics `
    tests

python -m pytest tests -q
```

Scope lint to project directories rather than `python -m pyflakes .`, which also scans the local virtual environment.
Do not edit installed dependencies to silence project lint output.

Run the tests under `tests/`; do not substitute execution of hardware diagnostics for the offline suite. Test counts can change as coverage evolves.

## Repository map

| Directory | Responsibility |
|---|---|
| `configs/` | Experiment profiles |
| `experiments/` | Planning, phase control, preconditioning execution, live entry point |
| `drivers/` | Instrument/session/channel and SCPI helpers |
| `acquisition/` | Spot/buffered/monitored acquisition and trace transports |
| `recording/` | Recording lifecycle, streaming CSV, and experiment manifests |
| `analysis/` | Offline evidence validation and calculation/export |
| `commissioning/` | Deliberate hardware qualification routines |
| `diagnostics/` | Developer hardware checks; some share helpers with other checks |
| `tests/` | Offline automated tests |

Commissioning and diagnostic scripts are not ordinary experiment steps. Some can connect to instruments or enable outputs. Read the specific script and its requirements before use; do not run every script as a smoke test.

## Current limitations

- Separate commands currently implement planning, live execution, validation, and per-phase export.
- Experiment-wide export and a unified command interface remain separate future changes.
- No automatic experiment resume/retry or external-supply control.
- Configuration validation checks consistency with supplied ratings, not correctness of those ratings.
- No README claim of hardware qualification for every combination of configuration, sampling rate, current, voltage, or DUT.
