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
