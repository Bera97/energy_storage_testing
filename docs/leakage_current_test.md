# Leakage-current test

One channel holds a constant voltage and the supply current is logged for at most 72 h.
The hold voltage is `charge.target_voltage_v`; the compliance (current limit) is the
resolved `charge.current`. The `rest` and `discharge` sections are required by the shared
schema and are ignored. Hardware execution is supervised and experimental
(`hardware_qualified` stays false).

## Commands

    python -m smu --config configs\leakage_current_trial_7_8_ch1.yaml
    python -m smu --config configs\leakage_current_trial_7_8_ch1.yaml --execute --analyze
    python -m smu --config configs\leakage_current_trial_7_8_ch1.yaml --analyze RUN_NAME

Run the short `configs\leakage_current_bench_check_ch1.yaml` first (10 minutes, 661 samples).
Keep the PC awake, on mains power and with automatic updates paused during a 72 h run.
Ctrl+C stops the run; outputs are shut down on every exit.

## Profile section `leakage`

- `maximum_duration_s`: at most 259200 (72 h); the last schedule segment must end there.
- `schedule`: segments `{until_s, interval_s}`. Samples are taken at fixed offsets from the
  start of the hold, plus one at t = 0. Total samples must not exceed 100000.
  The example schedule has 97741 samples.
- `early_stop`: off by default. When enabled the run stops after `consecutive_checks` checks,
  `check_interval_s` apart and not before `minimum_duration_s`, in which the mean current of
  the last `window_s` differs from the previous `window_s` by less than `relative_change`.
- `report_times_s`, `final_window_s`: where the analysis reports the current and over which
  final window the headline leakage current is averaged.
- `maximum_consecutive_read_failures`: failed reads tolerated per sample (default 2, range 0-10).
  Instrument fault flags are never retried.

## Run folder

`leakage_samples.csv` (flushed per sample, fsynced every 30 s) and `leakage_metadata.json`
(state `running` during the run; final state, error, shutdown verification and CSV SHA-256
afterwards). A run that is not finalized is a crashed or interrupted run.

## Stops

The run stops, shuts down and keeps partial data if a sample is more than max(2 x interval, 2 s)
late, the voltage leaves the DUT rating or exceeds the hold voltage plus
`charge.cv_voltage_tolerance_v`, the current exceeds 1.1 x compliance, a fault flag is
reported, or reads fail more often than allowed in a row.

## Analysis outputs

`leakage_summary.json`, `leakage_report_times.csv` and, with matplotlib, `leakage_current.png`.
The headline `leakage_current_a` is the final-window mean; a warning is added if that window still
contains current-limited samples. Stabilization uses the configured early-stop settings, or a
labelled reference criterion (24 h minimum, 2 h windows, 1 %, 30 min checks, 3 in a row).

## Current ranging

Automatic ranging of the SMU switches current ranges repeatedly as the current decays, and each
switch shifts the delivered voltage by roughly 0.1 mV. A large capacitive DUT turns that into
current jumps of orders of magnitude, so the default `auto` mode is not suitable for leakage
measurements. Use step-down-only ranging:

    leakage:
      range_control:
        mode: step_down

The instrument keeps automatic ranging while the source is in compliance (constant-current
charge-up). After the first reading outside compliance the program fixes the range, then steps
down one decade at a time, only after `confirm_samples` consecutive readings (the peak of them)
stay below `headroom` x the lower range. It steps up only after a measured overflow and then
refuses to step down for `overflow_lockout_s`. More than `maximum_range_increases` overflows
stop the run. Every change is listed in `leakage_metadata.json` under `range_control.events`
and the range in effect is stored per sample in the `range_a` column. The analysis reports the
number of range changes, warns if the range changed inside the final window and warns when
automatic ranging was used.

Options (all optional): `minimum_range_a` (default 1e-7), `headroom` (0.5), `confirm_samples` (20),
`overflow_lockout_s` (3600), `maximum_range_increases` (10).

## Range handling at start and end

If the very first reading overflows the instrument's current range (reported as 9.9E+37, which
the code reads as an invalid current field) in `step_down` mode, the program does not count it as a
read failure. It takes manual control immediately at the lowest range that covers the compliance
current (1 A for 0.4 A compliance), logs a `start_overflow` event and carries on stepping down
from there. In `auto` mode such a reading is still a read failure and stops the run after the
allowed number of consecutive failures.

After a step-down run the program switches the instrument back to automatic current ranging
(after the outputs are verified OFF) and records `auto_range_restored` in the run metadata, so
other experiments do not inherit a fixed low range. If the run is killed before it can do that,
the next leakage run starts from a known range, and a power cycle also restores automatic ranging.
