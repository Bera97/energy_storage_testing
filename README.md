# Energy storage testing

Supervised preconditioning experiments using a Keysight B2902B source-measure unit.
Run `python -m smu` from the repository root.
The procedure is charge, rest, discharge, rest, repeated for the configured cycle count.

## One command

All folder and file names come from the YAML profile, so one command covers the whole workflow.
`output.root_directory` holds the recordings; `output.analysis_directory` holds the analysis exports; `output.file_name` names each run folder as `<timestamp>_<file_name>`.
One `--config` runs one channel; two `--config` arguments run both channels of one instrument.

```powershell
# Preview: validate the plan, print it, no hardware (also the default without flags)
python -m smu --config .\configs\preconditioning.channel1.yaml --preview

# Execute: run the experiment on hardware
python -m smu --config .\configs\preconditioning.channel1.yaml --execute

# Execute and analyze: analyze the recorded run when it is over
python -m smu --config .\configs\preconditioning.channel1.yaml --execute --analyze

# Analyze an existing run: pass only the run folder name inside output.root_directory
python -m smu --config .\configs\preconditioning.channel1.yaml --analyze 20261110_2230_channel1

# Two channels on one instrument: pass both profiles
python -m smu --config .\configs\preconditioning.channel1.yaml --config .\configs\preconditioning.channel2.yaml --execute --analyze
```

Run folders are `<timestamp>_<file_name>` inside `output.root_directory`, so repeated runs never overwrite each other.
The analysis export for a run lands in `output.analysis_directory\<run folder name>` and is refused if it already exists.
The analysis export directory is created automatically; you no longer need to create it first.
`--execute` remains the explicit instruction to run hardware; `--preview` and `--analyze` never connect.
If a run fails, analysis is skipped and the failure manifest is preserved.
The public interface uses flags only. Legacy public subcommands such as `plan`, `run`, `dual-run`, and positional `analyze` are rejected.

## Safety and scope

This is experimental supervised bench software, not an unattended charger or an independently qualified protection system.
Review the actual specimen datasheet, ratings, polarity, wiring, sensing, instrument operating envelope, and independent protection before executing.
Planning validates supplied settings; it does not verify physical safety. Evidence validation does not establish calibration or electrical isolation.
VISA and filesystem calls can block, so software deadlines do not guarantee a hard shutdown response time.
There is no automatic retry, resume, or external-power-supply control. Preserve failed recordings.

`--execute` is the explicit instruction to run hardware. Without it, run commands preview only.
There are no interactive confirmations and no required specimen-ID confirmation arguments.
Do not run two single-channel controllers against the same instrument; pass both reviewed profiles to one invocation using two `--config` arguments.

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
The `output` section carries all folder names: `root_directory` for recordings, `analysis_directory` for analysis exports, and `file_name` for the `<timestamp>_<file_name>` run folder used by the one-command interface.

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
Use one reviewed YAML profile:
```powershell
python -m smu --config .\configs\preconditioning.channel1.yaml --preview
python -m smu --config .\configs\preconditioning.channel1.yaml --execute --analyze
```
Without a mode flag, the command previews only.
Run names are generated from the local timestamp and `output.file_name`.
Existing recording destinations are refused rather than overwritten.
To analyze an existing completed run, use its folder name inside
`output.root_directory`:
```powershell
python -m smu --config .\configs\preconditioning.channel1.yaml --analyze RUN_NAME
```
The export is created inside `output.analysis_directory`.
The analysis parent directory is created automatically; the export
destination itself must not already exist.
Whole-experiment analysis requires valid, completed, unchanged evidence.
## Two channels on one instrument
Use profiles for channels 1 and 2 with the same exact VISA address,
different specimen IDs, the same output root, and matching expected serials.
Each channel keeps its own phase progression, settings, rests, and cycle count.
```powershell
python -m smu --config .\configs\preconditioning.channel1.yaml --config .\configs\preconditioning.channel2.yaml --preview
python -m smu --config .\configs\preconditioning.channel1.yaml --config .\configs\preconditioning.channel2.yaml --execute --analyze
python -m smu --config .\configs\preconditioning.channel1.yaml --config .\configs\preconditioning.channel2.yaml --analyze RUN_NAME
```
The first profile supplies the generated run-name suffix and the analysis
locations used by the public interface. Review both profiles before execution.
Placeholder specimen IDs and zero discharge cutoff remain refused for live
dual execution. There is no public `--qualification-run` option.
The runner owns one shared instrument session. Normal transitions stop their
own channel; faults attempt stop-both cleanup and failure recording before closure.
Startup requires outputs OFF, explicitly sets independent trigger groups with
`:SYST:GRO:DEF (@1),(@2)`, waits for operation completion, and verifies the group
readback before channel setup.
Whole-batch analysis exports per-phase analysis, `summary.json`, `summary.csv`,
and `ANALYSIS_COMPLETE`.
The existing preview schema still contains legacy planning-only wording,
including `live_backend_available: false`. This refactor preserves those fields;
they are not a current inventory of implemented execution backends and do not
establish hardware readiness.
## Recordings and analysis

- `output.root_directory` controls the recording root; relative paths depend on the working directory.
- Single-channel experiments use `experiment.json`; dual batches use `dual_experiment.json`.
- Active recordings contain `metadata.json`, `measurements.csv`, and `COMPLETE` when successfully finalized.
- `DUAL_COMPLETE` records dual-batch finalization after successful cleanup and evidence validation.
- A finalized whole-single-experiment analysis contains combined summaries, per-phase exports, and `ANALYSIS_COMPLETE`. Individual phase-export API results contain `analysis.csv` and `summary.json` without that whole-export marker.
- Completion markers describe distinct lifecycle stages, not proof against later modification.
- Analyze only finalized, unchanged inputs. Validation is not a transactional filesystem snapshot.
- Signed quantities retain measured signs. Directional totals are nonnegative magnitudes, not automatically device-labelled delivered or absorbed energy.
- Integration covers the first-to-last recorded sample, not unrecorded transitions or rests.
- Compliance flags need interpretation in the context of source mode and phase; they are not automatically acquisition failures.
- References may be absolute. Copied recordings can retain original paths; do not silently rewrite evidence or guess another phase directory. Explicit phase-root overrides are available through the analysis APIs; the public flag interface does not expose that override.
- Keep raw evidence and generated analysis out of source commits. `.gitignore` does not untrack already committed files.

## Developer commands
```powershell
python -m pyflakes smu.py drivers acquisition experiments recording analysis checks tests
python -m pytest tests -q
git --no-pager diff --check
```
The public CLI calls operation functions directly instead of constructing
argument lists for backend parsers. Backend `main()` functions remain internal
developer/test entry points, not additional public `python -m smu` subcommands.
Offline simulation exercises virtual scheduling and synthetic endpoints.
It does not qualify electrochemistry, live timing, physical shutdown,
or recording durability.
Connected diagnostics can change instrument configuration. Use them only
deliberately, with their documented prerequisites, disconnected DUTs where
required, and no competing controller. Do not run hardware diagnostics
indiscriminately or lint the virtual environment.
Legacy argument resolution remains in internal backend interfaces where
retained by tests. Values duplicating YAML execution settings must agree with
YAML; conflicting values are rejected.
`session_close_returned` means the driver's close method returned, not
independent proof that every underlying resource closed.
## Layout

| Path | Responsibility |
|---|---|
| `smu.py` | Public flag parsing, operation dispatch, and single-run analysis presentation |
| `configs/` | Specimen-specific profiles |
| `experiments/` | Configuration, planning, phase decisions, and execution coordination |
| `experiments/settings.py` | Current and execution-setting resolution |
| `experiments/charge_setup.py` | Outputs-off charge source configuration and preparation |
| `experiments/discharge_setup.py` | Outputs-off discharge source configuration and preparation |
| `experiments/trace_timing.py` | First-sample timing and host-deadline adapters |
| `analysis/dual_experiment.py` | Whole-batch dual analysis export |
| `drivers/` | Instrument/session/channel and SCPI helpers |
| `acquisition/` | Acquisition and trace transports |
| `recording/` | CSV, metadata, manifests, finalization |
| `analysis/` | Offline validation, calculation, phase export, and single-experiment export |
| `checks/` | Deliberately invoked hardware diagnostics |
| `tests/` | Offline automated tests |
## Extension points and deferred experiments
The implemented procedure remains preconditioning:
CC-CV charge, rest, constant-current discharge, rest.
Future work includes separate-test sweeps of charging current, successful
fixed-duration CV holding, discharge current, and constant discharge power,
plus time-variable/pulsed current and power discharge.
These modes are not implemented by this structural refactor.
Changing YAML experiment or mode names does not enable them.
Keep future responsibilities separate:
- Planning selects test conditions and constructs phase sequences.
- Controllers determine phase setpoints and successful termination.
- Setup configures the instrument and acquisition with outputs off.
- Execution coordinates activation, monitoring, stopping, and finalization.
- Recording retains raw evidence; analysis interprets completed evidence.
Existing termination hold time means consecutive qualifying CV/current
observations. Maximum CV duration remains a failure timeout, not successful
completion of a timed CV hold.
