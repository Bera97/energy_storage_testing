# Energy storage testing

Recorded energy-storage experiments using a Keysight B2902B source-measure unit.

The public interface is `python -m smu`. Run commands from the repository root.

## Everyday commands

| Command | Purpose | Hardware behavior |
|---|---|---|
| `plan --config FILE` | Validate live-required settings and preview the plan | No connection or output enabling |
| `run --config FILE` | Preview by default; execute only with explicit authorization | Live execution requires `--execute` and confirmations |
| `check DIRECTORY` | Validate experiment evidence | Offline, read-only |
| `analyze DIRECTORY OUTPUT_DIRECTORY` | Export all active phases of a completed experiment | Offline; writes only new analysis outputs |
| `analyze-phase DIRECTORY OUTPUT_DIRECTORY` | Export one recorded active phase | Offline; writes only new analysis outputs |

```powershell
python -m smu --help
python -m smu run --help
python -m smu analyze --help
```

Run separate commands on separate lines. PowerShell backticks continue one command, and must not have trailing spaces.

## Safety and scope

A passing preview establishes consistency with supplied configuration, not electrical safety or correctness of the DUT ratings.
Verify the actual datasheet, polarity, wiring, sensing, instrument operating envelope, and independent DUT protection before execution.
Software/instrument shutdown evidence is not proof of physical isolation.

No automatic live retry, experiment resume, or external-power-supply control is provided.
Do not run checks/diagnostic scripts indiscriminately: some connect to hardware or enable outputs.

The committed `configs/preconditioning.test_run.yaml` contains specimen-specific settings and placeholder descriptive metadata. It is not a universally safe template.

## Environment

Keep the existing validated environment rather than upgrading it for this documentation change.

```powershell
.\.SMUvenv\Scripts\Activate.ps1
python --version
```

For a fresh environment only:

```powershell
python -m venv .SMUvenv
.\.SMUvenv\Scripts\Activate.ps1
python -m pip install PyYAML PyVISA pytest pyflakes
```

Use Python 3.11 or newer: the code uses `BaseException.add_note`.
This is an initial direct-dependency setup, not a pinned or independently qualified environment specification.
Live communication also requires a suitable VISA backend; installing PyVISA alone does not install a vendor VISA library. Retain the known-working instrument-PC setup.

## 1. Prepare a specimen-specific configuration

Copy an existing profile only if the destination does not already exist:

```powershell
if (-not (Test-Path .\configs\preconditioning.local.yaml)) {
    Copy-Item .\configs\preconditioning.test_run.yaml .\configs\preconditioning.local.yaml
}
```

Review all sections before use:

| Section | Review |
|---|---|
| `experiment` | Type, name, complete cycle count |
| `dut` | Specimen identity, category, real model/datasheet, capacity/capacitance, verified ratings |
| `instrument` | VISA address, channel, sensing arrangement, NPLC |
| `acquisition` | Sampling interval and maximum permitted sample gap |
| `charge` | Current basis/factor, target voltage, CV tolerance, termination reference/factor/hold, timeouts |
| `rest` | Durations after charge and discharge |
| `discharge` | Current basis/factor, cutoff voltage, timeout |
| `output` | Recording root directory |

Under `charge.current`, set exactly one of `current_a`, `current_fraction`, or `c_rate`; omit the others or set them to null. Absolute current is in amperes; fraction multiplies the supplied maximum charge-current rating. C-rate is available for battery, edlc, and lic and requires an explicit nominal_capacity_ah. Legacy basis/factor input remains supported but must not be mixed with the new fields. Discharge selection is unchanged. All selections remain subject to the current rating and termination-current checks. Inspect resolved currents in the plan.
Unknown fields and duplicate YAML keys are rejected.

Live-required monitoring includes `charge.cv_voltage_tolerance_v` and `acquisition.maximum_sample_gap_s`. The gap must be at least the sample interval and less than the termination hold time.

### Different profiles versus different procedures

Different compatible YAML profiles can select different specimens, settings, durations, and cycle counts using the same CLI.
Currently the implemented procedure is `experiment.type: preconditioning`, with charge/rest/discharge/rest cycles.
An unrelated procedure needs a dedicated validated planner/executor before a new `experiment.type` can be accepted. Changing the type string alone does not implement a new test.
The same CLI can remain the public interface as additional procedures are implemented.

## 2. Preview

```powershell
python -m smu plan --config .\configs\preconditioning.local.yaml
```

Review the specimen, resource, channel, resolved currents, phase order, monitoring, acquisition budgets, and required operator ceilings.
The preview reports `will_connect: false` and `will_enable_output: false` and does not create experiment recordings.
`plan` cannot be combined with `--execute`.

The default software planning limit is 100000 records. Planning does not query or independently qualify instrument capacity.

## 3. Explicit live execution

Template only: replace every placeholder after physical and configuration review. Numeric placeholders intentionally are not executable values.

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

Without `--execute`, `run` previews rather than executing.
The specimen confirmation must match the YAML exactly. The expected serial is checked against instrument identity.
Experiment IDs contain 1-80 ASCII letters, digits, underscores, or hyphens.

Review operator ceilings independently; do not blindly derive them from an inappropriate configuration. Discharge preparation uses the supplied DUT maximum voltage as voltage compliance, which can exceed the charge target.

Optional flags include `--maximum-records`, `--timeout-ms`, and `--visa-library`; inspect `run --help` before changing them.
Keep the reported `experiment_directory`.
On failure or interruption, preserve the manifest and partial recordings; do not automatically restart or mark them completed.

## 4. Validate the experiment

Use the actual experiment directory reported by the runner:

```powershell
$ExperimentDir = ".\runs\REPLACE_WITH_REPORTED_EXPERIMENT_DIRECTORY"
python -m smu check "$ExperimentDir"
```

For JSON:

```powershell
python -m smu check "$ExperimentDir" --json
```

| Exit code | Meaning |
|---|---|
| 0 | Valid completed experiment evidence |
| 1 | Invalid evidence or validation could not complete |
| 2 | Command-line usage error |
| 3 | Valid evidence, but experiment not completed |

Validation checks persisted consistency, not measurement accuracy, authenticity, or physical isolation.
Phase references must be absolute and within the allowed phase root. The default allowed root is the experiment directory's parent. Use `--phase-root` only when you deliberately need a different authorized root, not to bypass an unexplained error.
Copied recordings can retain absolute references to their original location; do not silently rewrite evidence or analyze a guessed directory.

## 5. Analyze the complete experiment

Only analyze recordings that are no longer being written or modified. Use a new output directory outside the experiment and its phase recordings.

```powershell
New-Item -ItemType Directory -Force .\analysis_outputs | Out-Null

python -m smu analyze `
    "$ExperimentDir" `
    .\analysis_outputs\REPLACE_WITH_NEW_EXPORT_NAME
```

The output parent must already exist; the export directory itself must not exist.
`analyze` requires a valid completed experiment. It follows manifest references, not a folder-name search.
It validates before export and revalidates before successful finalization, but these checks are not a transactional filesystem snapshot. Keep the inputs unchanged throughout.

A successful export contains:

```text
summary.csv
summary.json
ANALYSIS_COMPLETE
phase_<index>_charge_cc_cv/
    analysis.csv
    summary.json
phase_<index>_discharge_cc/
    analysis.csv
    summary.json
```

There is one per-phase directory per active phase, including additional cycles. Rest phases do not produce active-phase energy exports.
The console and combined table report phase identity, sample count, recorded span, signed charge, and signed energy; the CSV also includes directional energy magnitudes and warning counts. Full phase warnings remain in JSON.

Existing exports are not overwritten. An error can leave partial output; treat it as incomplete unless `ANALYSIS_COMPLETE` was written. A completion marker records export finalization, not proof against later modification.

Signed quantities retain measured signs. Positive and negative energies are nonnegative directional magnitudes, not automatically device-labelled delivered/absorbed energy. Integration covers the first-to-last recorded sample, not unrecorded transitions or rest periods.

### Analyze only one phase

For an individual manifest-referenced recording:

```powershell
python -m smu analyze-phase `
    REPLACE_PHASE_RECORDING_DIRECTORY `
    .\analysis_outputs\REPLACE_WITH_NEW_PHASE_EXPORT_NAME
```

Per-phase analysis is not proof that the overall experiment completed successfully.

## Recording and repository organization

`output.root_directory` controls the experiment output root; relative paths depend on the working directory.
The experiment manifest is `experiment.json`. Active recordings use `metadata.json`, `measurements.csv`, and, for successfully finalized recordings, `COMPLETE`.
Do not confuse recording finalization, experiment completion, and analysis completion.

| Path | Responsibility |
|---|---|
| `smu.py` | Public command routing and experiment-wide analysis |
| `configs/` | Experiment profiles |
| `experiments/` | Validation/planning and phase/preconditioning execution |
| `drivers/` | Instrument/session/channel and SCPI helpers |
| `acquisition/` | Acquisition and trace transports |
| `recording/` | Recording lifecycles, CSV, manifests |
| `analysis/` | Offline validation, calculation, per-phase export |
| `checks/` | Deliberate developer hardware checks |
| `tests/` | Offline automated tests |

Preserve raw evidence and failed recordings. Derived analysis should be separate.
Review data tracking deliberately; `.gitignore` does not untrack committed files.
Historical module-level entry points remain available for compatibility, but use `smu` for everyday work.

## Development checks

Run these as separate commands:

```powershell
python -m pytest tests/test_smu_cli.py -q
```

```powershell
python -m pyflakes `
    smu.py `
    drivers `
    acquisition `
    experiments `
    recording `
    analysis `
    checks `
    tests
```

```powershell
python -m pytest tests -q
```

Do not lint the entire virtual environment or run hardware diagnostics as a replacement for offline tests.
Test counts can change as coverage evolves.

## Source consolidation
The maintained diagnostic scripts are:
- `checks/check_visa.py`
- `checks/check_dc_resistor.py`
- `checks/check_streaming_sink_resistor.py`
- `checks/check_streaming_sink_duration.py`
- `checks/check_streaming_delayed_sink_cutoff.py`
Historical development probes were retired from the current source tree.
Their removal retires those individual manual commands; it does not
establish that every old check has an equivalent replacement.
The implementations remain in earlier Git revisions.
Charge/discharge polling interfaces now live in
`experiments/phase_polling.py`.
Prepared-phase recording coordination now lives alongside
`ExperimentPhaseRun` in `recording/experiment_phase_run.py`.
Existing polling endpoints, phase-specific exception-report attributes,
recording finalization, and original-error preservation remain unchanged.
## Dual-channel development: planning only
Preview two independent preconditioning profiles:
```powershell
python -m smu dual-plan --config configs/preconditioning.channel1.yaml --config configs/preconditioning.channel2.yaml
```
The profiles must select channels 1 and 2, use the same exact VISA
resource address, and have different specimen IDs.
Each profile retains its own device category and test settings.
The generated profiles are planning examples, not approved physical
test configurations. The channel-2 example copies the first profile's
ratings and requires review for the actual second specimen.
This command cannot connect, enable output, or execute experiments.
A shared-session concurrent execution backend is not implemented yet.
Do not launch two single-channel runners against the same instrument.
## Dual-channel coordinator simulation
The coordinator can be exercised without hardware:
```powershell
python -m smu dual-simulate --config configs/preconditioning.channel1.yaml --config configs/preconditioning.channel2.yaml
```
It uses virtual time and synthetic phase endpoints. It tests independent
progression, rest scheduling, endpoint acceptance, deadlines, finalization
failure, and shared cleanup. It does not simulate electrochemistry,
instrument timing, physical shutdown, measurement accuracy, or CSV durability.
A deliberate fault can be injected using `--fail-channel 1` or
`--fail-channel 2`; that command is expected to return a nonzero exit code.
All reports explicitly identify their evidence as simulated.
The CLI does not provide a real dual-channel execution backend.
Do not run two single-channel controllers against the same instrument.
## Dual-channel outputs-OFF probe
`python -m smu dual-probe --config FILE1 --config FILE2`
validates the profiles without constructing an instrument.
Adding `--connect --expected-serial SERIAL` opens one session,
verifies identity, attempts shutdown of both channels, configures
shared measurement formatting and per-channel NPLC while outputs
are OFF, verifies channel-local stopping, then attempts final
global shutdown and closes the session.
Use this only on an isolated bench with both DUTs disconnected and
no other controller using the instrument. It changes instrument
configuration; it is not a passive identity query.
The probe never enables output or starts acquisition.
It is not concurrent experiment execution or a timing qualification.
A stop readback does not establish electrical isolation.
`session_close_returned` means the existing driver's close method
returned; it is not independent proof that every resource closed.
The simulated coordinator remains simulation-only.
## Dual-channel trace acquisition development
`acquisition/dual_trace.py` configures only the selected channel's
finite timer/trace acquisition and reads at most one bounded batch
per poll. It does not enable outputs or provide a live experiment CLI.
Shared dual-session setup disables timer auto-reset while both outputs
are OFF. It does not reset the global timer. Individual phases use
their own recorded timestamp origins; host deadlines remain mandatory.
Active batches go through a supplied monitor callback. Trailing batches
after stopping use a separate saving callback so a completed monitor
is not re-entered. Recording finalization belongs to the caller.
Failures propagate to the owning coordinator, which must attempt
stop-both cleanup. A batch-size bound cannot interrupt a blocked VISA
operation and does not establish joint-channel timing qualification.
The simulated coordinator remains simulation-only. No energized dual
cell experiment is authorized by this increment.
