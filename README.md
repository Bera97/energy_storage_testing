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

Execution normally reads the expected serial from a concrete USB VISA address,
the voltage ceiling from `dut.ratings.maximum_voltage_v`, and the current
ceiling from the greater continuous charge/discharge current rating.
No duplicate execution section or extra command-line flags are required.
The connected instrument identity is still checked. These defaults use the
supplied ratings; they do not independently verify physical safety.
For non-USB resources, or to specify independent limits, this optional
top-level section overrides the defaults:

```yaml
execution:
  expected_serial: MY61391173
  maximum_voltage_v: 5.55
  maximum_current_a: 0.1
```

These are the current bench-example values, not universal limits. An explicit expected serial overrides the serial derived from the USB VISA address; the resolved serial is checked against the connected instrument.
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

## Constant-current discharge sweeps
Use `experiment.type: discharge_current_sweep` with fixed scalar charge
settings and the normal required experiment sections. Add:
```yaml
sweep:
  discharge_current_a: [0.4, 0.35]
  repeats: 1
  initial_discharge: true
```
These are positive current magnitudes in amperes, not universally safe limits.
Values must be finite, positive, distinct, within the supplied continuous
discharge rating, and valid under acquisition and execution limits.
Supplied order is preserved, with repeats grouped under each value.
Do not combine multiple sweep axes.
The scalar `discharge.current` basis/factor mapping selects the initial
preparation discharge current independently of the sweep list.
Initial discharge and its following rest occur once before the first condition.
Every condition then performs its configured complete charge/rest/discharge/rest
cycles with unchanged charge settings, cutoff, timeouts, and rest configuration.
Rest follows the selected mode: outputs off or explicit zero-current logging.
A common procedure does not independently establish identical physical state
of charge across conditions.
Requested amperes are expanded into the equivalent rated-discharge fractions
for the existing scalar backend. Inspect resolved ampere values in preview;
minor floating-point differences in the internal fractions are possible.
All conditions and initial preparation are planned before hardware connection.
A timeout or failed stage stops progression; there is no automatic retry/resume.
Preview the illustrative profiles without connecting:
```powershell
python -m smu --config configs/discharge_current_sweep.ch1.example.yaml --preview
python -m smu --config configs/discharge_current_sweep.ch1.example.yaml --config configs/discharge_current_sweep.ch2.example.yaml --preview
```
Use the existing `--execute --analyze` or offline `--analyze RUN_NAME`
interface with reviewed profiles. Examples contain placeholder identities.
Dual sweeps require the same experiment type on both channels, distinct
specimens, matching resources and output locations, and one shared session.
Both initial preparations must complete before either channel charges.
Subsequent progression is independent; a fault attempts stop-both cleanup.
Analysis includes `discharge_current_a` in phase/cycle comparisons and
`discharge_current_summary.csv`, grouped by discharge and charge current.
Dual statistics remain separate by channel/specimen. Initial preparation is
excluded from condition comparisons. On a charge-phase row,
`discharge_current_a` labels the condition's later discharge, not measured
charging current. Signed charge retains its measurement sign; directional
energy magnitudes and recorded energy ratios are not independently established
device efficiency. Recorded spans cover samples, not exact output-enable time.
Hardware evidence recorded on 2026-11-24:
`20261124_155335_discharge_current_trial_ch1`, specimen `7&8`, channel 1,
initial discharge 0.4 A, charging 0.4 A, and one condition each at 0.4 A
and 0.35 A discharge. All 10 phases completed; saved validation checked five
active recordings, all discharge endpoints were `voltage_min`, and all three
stage sessions recorded verified shutdown and successful closure.
Charge-compliance warnings remain recorded and require phase-aware interpretation.
This is limited single-channel evidence, not dual-channel qualification or
independent verification of physical ratings, calibration, or protection.
## Developer commands
```powershell
python -m pyflakes smu.py drivers acquisition experiments recording analysis checks tests
python -m pytest tests -q
git --no-pager diff --check
```
The public experiment CLI is `python -m smu`; it calls operation functions
directly rather than constructing argument lists for backend parsers.
The legacy `python -m experiments` planning entry point and the parsers in
`experiments.run_live` and `experiments.dual_preconditioning` have been retired.
Their planning, preview, and execution APIs remain available.

Retained standalone developer tools include:
- `python -m experiments.dual_coordinator`: hardware-free simulation, including
  synthetic phase duration and channel-failure injection.
- `python -m experiments.dual_instrument_io`: session probe; without `--connect`
  it plans only. With `--connect` it communicates with the instrument and
  changes configuration while verifying outputs OFF; review its prerequisites.

Use `--help` on these standalone modules to inspect their arguments.
They are not `python -m smu` subcommands.
The legacy dual-execution parser has also been retired; use the public
`smu` interface or `experiments.dual_live.run_configured()` for execution.
The internal `experiments.dual_live.main(["dual-check", DIRECTORY])`
interface remains available for read-only batch validation.
Other offline validation and analysis interfaces remain unchanged.
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
| `smu.py` | Public flag parsing and operation dispatch |
| `configs/` | Specimen-specific YAML profiles |
| `experiments/config.py` | Configuration loading and schema validation |
| `experiments/settings.py` | Current and execution-setting resolution |
| `experiments/numeric.py` | Shared numeric validation policies |
| `experiments/charge_setup.py` | Outputs-off charge configuration and preparation |
| `experiments/discharge_setup.py` | Outputs-off discharge configuration and preparation |
| `experiments/trace_timing.py` | Measurement-time mapping and host-deadline adapters |
| `experiments/` | Planning, phase controllers, monitoring, and execution coordination |
| `drivers/` | Instrument/session/channel I/O and SCPI helpers |
| `acquisition/monitored_acquisition.py` | Monitored acquisition and streaming CSV subclass |
| `acquisition/phase_trace_transport.py` | Shared charge/discharge trace lifecycle |
| `acquisition/` | Spot, buffered, monitored, and dual trace acquisition |
| `recording/run_data.py` | Snapshot export and destination preparation |
| `recording/` | CSV, metadata, manifests, and recording finalization |
| `analysis/export_experiment.py` | Single-experiment export and shared console presentation |
| `analysis/dual_experiment.py` | Persisted dual-batch validation and analysis export |
| `analysis/` | Offline evidence validation and numerical analysis |
| `checks/` | Explicit developer diagnostics; not run automatically |
| `tests/` | Offline regression tests |
## Extension points and deferred experiments
Implemented experiment types include preconditioning, charging-current sweeps,
CV-hold duration sweeps, termination-current-factor sweeps, and constant-current
discharge sweeps. See their configuration and execution sections.
The scalar cycle remains CC-CV charge, rest, constant-current discharge, rest.
Deferred work includes constant-power discharge sweeps, selectable current/power
consumption profiles with repeat-until-cutoff behavior, and dedicated leakage
tests. Changing YAML names alone does not implement these procedures.
Keep responsibilities separate:
- Planning selects conditions and constructs phase sequences.
- Controllers determine phase setpoints and successful termination.
- Setup configures instrument and acquisition with outputs off.
- Execution coordinates activation, monitoring, stopping, and finalization.
- Recording retains evidence; analysis interprets completed evidence.
Charge termination follows the selected `charge.termination.mode`.
Protective maximum charge/CV durations remain failure timeouts, not successful
holding periods.
## Charging-current sweep planning
`experiments/sweeps.py` contains sweep validation, expansion, and planning.
The existing preconditioning planner remains unchanged.
Use the complete example profile:
```powershell
python -m smu --config .\configs\charge_current_sweep.example.yaml --preview
```
Without mode flags, preview is also the default. The example retains the
normal experiment sections, sets `experiment.type: charge_current_sweep`,
and adds:
```yaml
sweep:
  charge_current_a: [0.1, 0.2, 0.4]
  repeats: 1
  initial_discharge: true
```
These are planning examples, not independently verified safe limits.
Each current expands into the requested number of separate planned
experiments. Values retain their supplied order, with repeats grouped
per current. `experiment.cycles` remains cycles within each experiment.
Duplicate current values are rejected; use repeats instead.
A plan is limited to 1000 condition/repeat entries.
Only the charging-current selection is replaced. Termination-reference
rules remain unchanged, so termination current can vary with charge
current. Inspect every condition's resolved settings and acquisition budget.
All conditions must validate before a plan is returned. Preview creates
no recording directories and does not authorize execution or establish a
comparable starting state. Sweep execution and combined analysis are
explicitly rejected in this increment. Normal preconditioning is unchanged.
### Initial discharge for charging-current sweeps
`sweep.initial_discharge: true` is required. The plan begins with one
discharge using the configured discharge current, voltage cutoff, and
maximum duration, followed by `rest.after_discharge_s` with outputs OFF.
This preparation occurs once before the first condition, not before
every repeat. Every condition still contains its configured complete
charge/rest/discharge/rest cycles.
The initial stage is separately identified in preview. Its phase
identities retain the canonical discharge/rest indices, rather than
pretending it is a complete preconditioning cycle. Total phase count
includes these two additional phases.
Execution must stop if initial discharge or its rest fails. Planning
does not establish physical readiness or guarantee identical specimen
state across conditions. Sweep execution is available through explicit `--execute`. Combined sweep
analysis remains unsupported; preview itself never connects to hardware.
### Sequential charging-current sweep execution
Current support: preview and explicit hardware execution. Combined sweep
analysis, automatic retry, and resume are not implemented.
Preview:
```powershell
python -m smu --config configs/charge_current_sweep.example.yaml --preview
```
Execute only after reviewing and replacing example profile values:
```powershell
python -m smu --config YOUR_REVIEWED_SWEEP_PROFILE.yaml --execute
```
A sweep reserves `<output.root_directory>/<timestamp>_<file_name>/sweep.json`
before any connection. Its `initial_discharge` folder records the initial
discharge and post-discharge rest. Condition folders then record complete
preconditioning sequences in supplied-current order, with repeats grouped
under each current.
Each stage has its own identity-verified instrument session. The existing
live executor attempts shutdown and closes that session before finalizing
the stage. Persisted recordings, the configured plan, and session evidence
are checked before the next stage starts. Initial preparation uses only
canonical discharge/rest phases; it does not perform a preceding charge.
A failure or interruption stops the sweep. Completed stages remain recorded;
the failed stage is marked raised and subsequent stages not_started. A
failure to persist progress also prevents further execution. There is no
automatic recovery or overwrite of an existing sweep directory.
The authoritative sweep result is the state in sweep.json. A running
manifest is not completion evidence, including after process termination.
Atomic metadata replacement does not guarantee power-loss durability.
Recorded shutdown evidence is not independent proof of electrical isolation.
`--execute --analyze` and sweep `--analyze RUN_NAME` are rejected before
hardware execution. Individual condition folders remain ordinary
preconditioning recordings; the initial discharge requires explicit
preparation-scope validation. Do not modify recordings during validation.
### Combined charging-current sweep analysis ? current support
This section supersedes earlier notes that sweep analysis is unsupported.
```powershell
python -m smu --config YOUR_REVIEWED_SWEEP_PROFILE.yaml --execute --analyze
python -m smu --config YOUR_SWEEP_PROFILE.yaml --analyze RUN_NAME
```
Execution followed by analysis runs analysis only after successful execution.
Analysis alone is offline. Set output.analysis_directory in the profile.
Existing analysis destinations are refused rather than overwritten.
Sweep validation reconstructs the condition plan from stored configuration,
checks initial preparation and condition order, restricts stage paths, and
revalidates every stage's recordings and instrument-session completion.
The analysis destination contains:
- summary.json: combined results, provenance, validation, and warnings.
- summary.csv: active-phase metrics including initial preparation.
- cycle_comparison.csv: paired charge/discharge metrics per condition/cycle.
- current_summary.csv: descriptive statistics grouped by configured current.
- Separate initial_discharge and condition analysis subdirectories.
- A top-level ANALYSIS_COMPLETE only after successful combined export.
Initial preparation is excluded from current comparisons. Signed quantities
retain measurement signs. Directional energy columns contain nonnegative
magnitudes. recorded_energy_ratio divides discharge negative-energy magnitude
by charge positive-energy magnitude over recorded samples; zero-denominator
ratios are empty. This is not an independently established device efficiency.
Recorded spans exclude unrecorded time before the first sample and must not be
reported as output-enable durations. Rest contributes no measured energy here.
Grouped statistics count cycle observations; they do not assert that cycles
within a condition are independent replicates. A single observation has no
sample standard deviation.
Do not modify recordings while validating or exporting. Manifests are checked
again, but these reads do not constitute a filesystem snapshot. Failed exports
can leave partial output without a top-level ANALYSIS_COMPLETE.
### Dual-channel charging-current sweeps
All experiment development must cover both single- and dual-channel operation.
Charging-current sweeps support two reviewed sweep profiles through one shared
instrument session. Do not run two single-channel sweep processes against one SMU.
```powershell
python -m smu --config YOUR_CH1_SWEEP.yaml --config YOUR_CH2_SWEEP.yaml --preview
python -m smu --config YOUR_CH1_SWEEP.yaml --config YOUR_CH2_SWEEP.yaml --execute --analyze
python -m smu --config YOUR_CH1_SWEEP.yaml --config YOUR_CH2_SWEEP.yaml --analyze RUN_NAME
```
Use channel 1 and channel 2, distinct specimen IDs, the same VISA resource and
expected serial, and matching recording/analysis roots. Each profile can specify
its own charging-current list, repeats, cycles, discharge settings, and rests.
Both channels must complete initial discharge and following rest before either
starts charging. Thereafter phase progression is independent, not lockstep.
A completed channel remains stopped while its peer continues. A fault invokes
the existing stop-both, interrupted-recording finalization, and session-close
handling. There is no automatic retry or resume.
The dual_experiment.json manifest records both complete sweep sequences.
DUAL_COMPLETE marks successful dual-run finalization. Analysis includes channel
and specimen identity, per-condition cycle comparisons, per-current statistics
kept separate by channel, and multi-unit console summaries. Initial preparation
is excluded from condition comparisons. ANALYSIS_COMPLETE is written last.
Dual operation still requires qualification of physical wiring and the joint
instrument operating envelope. Offline tests use fake instrument operations.
CV-hold requirement for future implementation: an additional hold after reaching
the charging-current fraction starts immediately at the first valid strictly
below-threshold current sample with CV confirmed, without a qualification delay.
This dual-sweep change does not yet implement either new CV-hold policy.
### Unified charge termination
Every experiment YAML profile uses the same charge.termination structure.
There is no separate charge.cv_hold setting in the maintained profiles.
```yaml
charge:
  # Retain mode, current, target_voltage_v, and other required charge settings.
  termination:
    mode: current_threshold
    current_reference: configured_charge_current
    current_factor: 0.1
    hold_time_s: 5
  maximum_duration_s: 3600
  maximum_cv_duration_s: 1800
```
The four termination fields have the same structure in single-channel,
dual-channel, preconditioning, and sweep profiles:
- current_threshold: hold_time_s is consecutive qualifying CV/current time.
- from_cv_entry: hold_time_s is total time from the first confirmed-CV sample.
  The current fraction defines a diagnostic threshold, not the stopping rule.
- after_current_threshold: hold_time_s is additional time starting immediately
  at the first valid strictly below-threshold current sample with CV confirmed.
  There is no qualification delay, and a subsequent current rise does not reset
  the timer while CV remains confirmed.
For a 30-second total CV hold:
```yaml
termination:
  mode: from_cv_entry
  current_reference: configured_charge_current
  current_factor: 0.1
  hold_time_s: 30
```
For 30 additional seconds after first crossing the charging-current fraction:
```yaml
termination:
  mode: after_current_threshold
  current_reference: configured_charge_current
  current_factor: 0.1
  hold_time_s: 30
```
after_current_threshold requires configured_charge_current as its reference.
Both timed policies fail if confirmed CV is lost after the timer starts.
The protective maximum_duration_s and maximum_cv_duration_s remain outside
termination because reaching them is failure, not successful completion.
A selected holding duration must leave room before the applicable timeout.
The planner derives internal phase fields and timing evidence from this one
termination block. Those derived fields are not additional YAML settings.
Old layouts remain readable for historical configuration/recording compatibility.
Do not combine termination.mode with the old charge.cv_hold block: that ambiguity
is rejected. Existing recordings and stored configuration snapshots are unchanged.
```powershell
python -m smu --config configs/cv_hold.ch1.example.yaml --config configs/cv_hold.ch2.example.yaml --preview
```
The example specimen IDs are preview-only placeholders. Offline tests and preview
do not establish physical ratings, wiring, or hardware readiness.
### CV-hold duration sweeps
Use experiment.type: cv_hold_sweep. The duration list is configured only at
charge.termination.hold_time_s; do not add a second duration list under sweep.
```yaml
experiment:
  type: cv_hold_sweep
  name: cv_hold_duration_comparison
  cycles: 1
charge:
  # Retain the other required charge settings.
  termination:
    mode: from_cv_entry
    current_reference: configured_charge_current
    current_factor: 0.1
    hold_time_s:[15][30][60]
sweep:
  repeats: 1
  initial_discharge: true
```
The same structure supports after_current_threshold. current_threshold is not
a timed-CV sweep mode. Each duration must be positive, distinct, and shorter
than the protective CV timeout. All conditions validate before execution.
The supplied charging current remains fixed; only the selected holding time
changes. Repeats are grouped under each supplied duration.
Single-channel execution uses one profile. Dual execution uses two CV-hold
sweep profiles and one shared instrument session. Channels can select different
timed policies, duration lists, repeats, and cycles. Mixed sweep experiment
types in one dual invocation are rejected. Both channels complete initial
preparation before either charges; subsequent progression is independent.
```powershell
python -m smu --config configs/cv_hold_sweep.ch1.example.yaml --preview
python -m smu --config configs/cv_hold_sweep.ch1.example.yaml --config configs/cv_hold_sweep.ch2.example.yaml --preview
```
The new examples retain preview-only specimen placeholders. Their values are
not independently qualified physical operating limits.
Analysis includes cv_hold_mode and cv_hold_time_s in per-condition comparisons.
hold_time_summary.csv groups observations by selected holding duration and
policy, separately for each dual channel/specimen. It does not pool different
durations merely because their charging currents match. Console summaries keep
mAh, C, Wh, and J and additionally show Hold(s).
Existing charging-current sweep interfaces and output names remain supported.
Historical recordings are not modified. Physical CV-hold validation is separate
from the offline integration tests.

## Optional recorded zero-current rest

Omitting rest.mode preserves the existing outputs-OFF rest and old plan shape.
The explicit outputs_off mode also performs no measurements. To select the new
output-enabled measurement procedure, use:

```yaml
rest:
  # Options: outputs_off | zero_current_logging.
  mode: zero_current_logging
  maximum_abs_current_a: 0.001
  after_charge_s: 5
  after_discharge_s: 30
```

The 0.001 A guard above is an example operator-reviewed SOFTWARE fault limit,
not an independently qualified current limit or a recommended universal value.
The guard must be positive and below both supplied continuous current ratings.
Existing voltage limits and configured sensing remain applicable. Do not infer
assembly current ratings merely by adding component ratings.

This procedure commands a fixed 0 A CURRENT source with the selected output ON.
It is not a 0 V voltage source, an open output relay, or electrical isolation.
The software requires exactly zero current-setpoint readback; it does not
silently substitute a small nonzero setpoint. Source mode, fixed DC operation,
voltage compliance, sensing, NPLC and output states are checked. Real hardware
behaviour, accuracy, transients and the operating envelope need supervision
and separate physical validation. No real hardware is exercised by the patch.

The recording uses paired spot queries through the existing SpotAcquisition
path, not a new buffered trace. The interval is a requested host scheduling
interval, not a hardware-guaranteed sample rate. Host query brackets are stored
along with raw instrument timestamps. Continuity between independent spot-query
instrument timestamps is NOT assumed. Gap checks use host query-completion
times, including the first query's latency from before output activation.
Timing out cannot interrupt a blocked VISA call. The duration endpoint is an
eligible sample at or after the requested host-relative rest duration, subject
to the configured gap/deadline. The physical output-enable instant is not
independently established. Slow communications may require a reviewed gap or
interval adjustment; do not disable guards to hide acquisition failures.

All three rest positions are supported: after initial discharge, after charge,
and after discharge. The single runner logs rests through its existing session.
Dual operation uses cooperative channel-local rest tasks, without changing
shared result formatting while a peer is active. Normal completion stops only
the selected dual channel; exceptions propagate to stop-both cleanup. The
initial-preparation barrier remains in effect. Preview and analysis are offline.

Rest recordings are explicitly separate sidecars: rest_phase_NNNN/rest.json,
measurements.csv and COMPLETE. They are not ordinary charge/discharge recordings;
use whole-experiment/sweep validation for them, not validate_run on a rest folder.
Completion requires closed CSV, source/activation/stop evidence and duration and
sample checks. CSV hashes detect subsequent data changes, not file authenticity.
Whole-run analysis validates and exports rest recordings and rest_summary.csv,
including voltage change and maximum absolute measured current. They remain
excluded from charge/discharge pairing and efficiency/energy-ratio calculations.
Current means SMU-terminal current, NOT internal LIC self-discharge or current
redistribution between parallel LICs. Raw evidence is never rewritten by export.
Analysis exports may contain original absolute recording references.

The new example has a REPLACE_ run-name prefix to prevent accidental execution
through the public CLI. It is for preview and review only. Existing trial
profiles are NOT enabled or changed by this patch. Keep their physical-test
history intact. Validate the single small case before larger CV sweeps or dual
physical trials. Existing failed runs are never automatically resumed.


## Experimental constant-power planning and controller

This increment adds `experiment.type: constant_power_discharge_sweep` for
PREVIEW ONLY. Both `--execute` and `--analyze` are explicitly rejected for
this experiment type, for one or two channels. Existing experiment types
and their execution paths are unchanged. This is not a live CP backend.

```powershell
python -m smu --config configs/constant_power_sweep.ch1.example.yaml --preview
python -m smu --config configs/constant_power_sweep.ch1.example.yaml --config configs/constant_power_sweep.ch2.example.yaml --preview
```

`sweep.power_w` contains unique positive power targets in supplied order;
`sweep.repeats` and `sweep.initial_discharge: true` retain their usual meanings.
The normal scalar `discharge` section selects constant-current initial
preparation, cutoff and timeouts. Each later condition plans fixed charging,
rest, a new `discharge_cp` phase, and the configured post-discharge rest.
This planning schema is experimental and may evolve before live integration.

`power_control` requires `maximum_current_a`, `update_interval_s`,
`maximum_feedback_age_s`, `relative_tolerance`, `absolute_tolerance_w`,
`startup_grace_s`, and `maximum_tracking_error_s`. Every condition must satisfy
`power_w / cutoff_voltage_v <= maximum_current_a`; zero cutoff is unsupported.
The current ceiling cannot exceed the supplied continuous discharge rating or
resolved execution ceiling. This feasibility check does not qualify instrument
sink capability, real-time behavior, overshoot or measured power accuracy.

`experiments.constant_power.ConstantPowerController` is a pure decision engine.
It proposes `I_command = -P_requested / V_fresh`; measured discharge power is
`-V * I`. It requires a fresh outputs-off starting sample, explicit setpoint
readback acknowledgement, and subsequent host-timed samples acquired after
that acknowledgement. Old trace batches must not be relabeled as fresh samples.
Current/voltage violations, wrong active source mode/polarity, compliance,
faults, stale or nonmonotonic feedback, timeouts and prolonged tracking errors
are unsuccessful terminal events. It never clamps current and silently calls
that constant power. The cutoff is the only successful endpoint.

The controller sends NO commands, enables NO outputs and performs NO shutdown.
A future recorded adapter must apply/read back setpoints, validate status and
sampling provenance, record samples and commands, poll the watchdog, stop both
channels on shared-session faults, finalize evidence, and attempt output-off
cleanup on every exit. A blocked VISA call can defeat host deadlines.
Single/dual previews do not constitute instrument or physical qualification.

The examples contain placeholder identities and illustrative powers/control
windows. Do not turn them into hardware runs by renaming modes. Native CP
capability is not assumed or established by this increment. Live acquisition
integration, CP recording validation/export, and supervised hardware trials
remain to implement. Later consumption profiles should reuse the controller;
profile scheduling and leakage testing are not part of this change.
