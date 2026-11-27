"""Offline analysis of one recorded leakage-current hold. Never opens an instrument."""
from bisect import bisect_left, bisect_right
import csv
import hashlib
from itertools import islice
import json
import math
from pathlib import Path
import statistics

from experiments.leakage_planning import sample_times
from experiments.leakage_runner import COLUMNS, CSV_NAME, METADATA_NAME, evaluate_stabilization

SUMMARY_NAME = "leakage_summary.json"
REPORT_NAME = "leakage_report_times.csv"
PLOT_NAME = "leakage_current.png"
FINAL_STATES = ("completed", "stopped_early")
ACCEPTED_COLUMNS = (COLUMNS, COLUMNS[:-1], COLUMNS[:-2])
REFERENCE_CRITERION = {
    "minimum_duration_s": 86400.0, "window_s": 7200.0, "relative_change": 0.01,
    "check_interval_s": 1800.0, "consecutive_checks": 3,
}
REPORT_COLUMNS = (
    "requested_s", "reached", "nearest_sample_s", "nearest_current_a",
    "window_mean_current_a", "window_samples", "window_in_compliance_samples",
)


def _load(source):
    metadata = json.loads((source / METADATA_NAME).read_text(encoding="utf-8"))
    if not isinstance(metadata, dict) or metadata.get("leakage_run_schema_version") != 1:
        raise ValueError("Unsupported or missing leakage run metadata.")
    with open(source / CSV_NAME, newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        if tuple(reader.fieldnames or ()) not in ACCEPTED_COLUMNS:
            raise ValueError("Leakage CSV columns differ from the recorded format.")
        rows = list(reader)
    try:
        table = {
            "t": [float(row["scheduled_s"]) for row in rows],
            "v": [float(row["voltage_v"]) for row in rows],
            "i": [float(row["current_a"]) for row in rows],
            "compliance": [int(row["in_compliance"]) for row in rows],
            "range": [float(row["range_a"]) if row.get("range_a") else None for row in rows],
            "flag": [int(row["after_range_change"]) if row.get("after_range_change") else 0
                     for row in rows],
        }
    except (TypeError, ValueError) as error:
        raise ValueError(f"Leakage CSV contains an unreadable value: {error}") from error
    return metadata, table


def _sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _stats(times, values, compliance, start, end):
    low = bisect_left(times, start)
    high = bisect_right(times, end)
    chunk = values[low:high]
    if not chunk:
        return None
    return {
        "start_s": times[low], "end_s": times[high - 1], "samples": len(chunk),
        "mean_current_a": statistics.fmean(chunk),
        "std_current_a": statistics.stdev(chunk) if len(chunk) > 1 else 0.0,
        "minimum_current_a": min(chunk), "maximum_current_a": max(chunk),
        "in_compliance_samples": sum(compliance[low:high]),
    }


def _report(times, currents, compliance, targets, window_s, last_s):
    entries = []
    for target in targets:
        entry = dict.fromkeys(REPORT_COLUMNS)
        entry["requested_s"] = target
        entry["reached"] = target <= last_s + 1e-9
        if entry["reached"]:
            k = bisect_left(times, target)
            nearest = min((j for j in (k - 1, k) if 0 <= j < len(times)),
                          key=lambda j: abs(times[j] - target))
            entry.update(nearest_sample_s=times[nearest], nearest_current_a=currents[nearest])
            stats = _stats(times, currents, compliance, max(0.0, target - window_s), target)
            if stats is not None:
                entry.update(
                    window_mean_current_a=stats["mean_current_a"],
                    window_samples=stats["samples"],
                    window_in_compliance_samples=stats["in_compliance_samples"],
                )
        entries.append(entry)
    return entries


def find_stabilization(times, currents, criterion):
    """Replay the early-stop rule over the recorded data."""
    check = criterion["minimum_duration_s"]
    last = times[-1]
    streak = 0
    streak_start = None
    evaluated = 0
    while check <= last + 1e-9:
        end = bisect_right(times, check)
        result = evaluate_stabilization(times[:end], currents[:end], check, criterion["window_s"])
        if result is not None:
            evaluated += 1
            if result[2] < criterion["relative_change"]:
                if streak == 0:
                    streak_start = check
                streak += 1
                if streak >= criterion["consecutive_checks"]:
                    return {"found": True, "stable_since_s": streak_start,
                            "confirmed_at_s": check, "checks_evaluated": evaluated,
                            "relative_change_at_confirmation": result[2]}
            else:
                streak = 0
        check += criterion["check_interval_s"]
    note = None
    if last < criterion["minimum_duration_s"]:
        note = "Recorded duration is shorter than the criterion minimum_duration_s."
    return {"found": False, "checks_evaluated": evaluated, "note": note}


def _plot(path, times, voltages, currents):
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        return False
    figure, (top, bottom) = plt.subplots(2, 1, sharex=True, figsize=(9, 6))
    top.plot(times[1:], [value * 1e6 for value in currents[1:]], linewidth=0.8)
    top.set_xscale("log")
    top.set_ylabel("Supply current (uA)")
    top.grid(True, which="both", alpha=0.3)
    bottom.plot(times[1:], voltages[1:], linewidth=0.8)
    bottom.set_ylabel("Terminal voltage (V)")
    bottom.set_xlabel("Time since hold start (s)")
    bottom.grid(True, which="both", alpha=0.3)
    figure.tight_layout()
    figure.savefig(path, dpi=150)
    plt.close(figure)
    return True


def analyze_leakage_run(source, destination, *, expected_document=None):
    source, destination = Path(source), Path(destination)
    metadata, table = _load(source)
    if expected_document is not None and metadata.get("configuration") != json.loads(
            json.dumps(expected_document)):
        raise ValueError("CLI profile differs from the recorded leakage profile.")
    leak = metadata["leakage"]
    t, v, i, compliance = table["t"], table["v"], table["i"], table["compliance"]
    ranges = table["range"]
    flags = table["flag"]
    keep = [k for k in range(len(t)) if not flags[k]]
    tk, vk, ik, ck = ([column[k] for k in keep] for column in (t, v, i, compliance))
    n = len(t)
    state = metadata["state"]
    stored_hash = metadata.get("csv_sha256")
    expected_times = list(islice(sample_times(leak["segments"]), n))
    checks = {
        "row_count_matches": None if state == "running" else n == metadata.get("recorded_sample_count"),
        "csv_sha256_matches": None if stored_hash is None else _sha256(source / CSV_NAME) == stored_hash,
        "schedule_matches_plan": len(expected_times) == n and all(
            math.isclose(a, b, rel_tol=0.0, abs_tol=1e-9) for a, b in zip(t, expected_times)),
        "times_increasing": all(b > a for a, b in zip(t, t[1:])),
    }
    valid = n > 0 and all(value is not False for value in checks.values())
    warnings = []
    if state not in FINAL_STATES:
        warnings.append(f"Run state is {state}; the data are partial.")
    if metadata.get("shutdown_verified") is not True:
        warnings.append("Shutdown was not verified for this run.")
    if metadata.get("read_retry_count"):
        warnings.append(f"{metadata['read_retry_count']} read retries occurred; see the run metadata.")

    summary = {
        "leakage_analysis_schema_version": 1,
        "run_name": source.name,
        "state": state,
        "complete": state in FINAL_STATES and metadata.get("shutdown_verified") is True,
        "valid": valid,
        "checks": checks,
        "specimen_id": metadata["specimen_id"],
        "channel": metadata["channel"],
        "hold_voltage_v": metadata["hold_voltage_v"],
        "compliance_current_a": metadata["compliance_current_a"],
        "samples": n,
        "recorded_duration_s": t[-1] if n else None,
        "initial_current_a": i[0] if n else None,
        "final_window": None, "leakage_current_a": None,
        "leakage_current_basis": "mean supply current over the final window",
        "in_compliance_samples": sum(compliance),
        "last_in_compliance_s": None,
        "report_times": [], "stabilization": None,
        "read_retry_count": metadata.get("read_retry_count"),
        "maximum_lateness_s": metadata.get("maximum_lateness_s"),
        "plot_written": False,
        "range_control": metadata.get("range_control"),
        "range_changes": None,
        "range_change_samples_excluded": sum(flags),
        "warnings": warnings,
        "limitations": [
            "Offline replay of recorded evidence; it does not qualify the instrument or DUT.",
            "Early charge-up samples are current-limited; interpret late-time current only.",
            "Timestamps are host-scheduled offsets; instrument time is stored in the CSV.",
            "Samples read right after a range change are left out of window means and the stabilization replay.",
        ],
    }
    if n:
        flagged = [t[k] for k in range(n) if compliance[k]]
        summary["last_in_compliance_s"] = flagged[-1] if flagged else None
        window_s = leak["final_window_s"]
        final = _stats(tk, ik, ck, max(0.0, t[-1] - window_s), t[-1])
        final["requested_s"] = window_s
        volts = vk[bisect_left(tk, max(0.0, t[-1] - window_s)):]
        final.update(mean_voltage_v=statistics.fmean(volts), minimum_voltage_v=min(volts),
                     maximum_voltage_v=max(volts))
        changes = [k for k in range(1, n) if ranges[k] != ranges[k - 1]]
        in_final = sum(1 for k in changes if t[k] >= t[-1] - window_s)
        summary["range_changes"] = {"total": len(changes), "in_final_window": in_final}
        if in_final:
            warnings.append("The current measurement range changed inside the final window; "
                            "the headline value mixes ranges.")
        if (metadata.get("range_control") or {}).get("mode") != "step_down":
            warnings.append("Automatic current ranging was used; range switches can shift the "
                            "delivered voltage and with it the current.")
        summary["final_window"] = final
        summary["leakage_current_a"] = final["mean_current_a"]
        if final["in_compliance_samples"]:
            warnings.append(
                "The final window contains current-limited (compliance) samples; "
                "the headline leakage current is not a clean hold value."
            )
        summary["report_times"] = _report(tk, ik, ck, leak["report_times_s"], window_s, t[-1])
        early = leak["early_stop"]
        if early.get("enabled"):
            criterion = {key: early[key] for key in REFERENCE_CRITERION}
            source_name = "configured_early_stop"
        else:
            criterion, source_name = dict(REFERENCE_CRITERION), "reference_criterion"
        summary["stabilization"] = {"source": source_name, "criterion": criterion,
                                    **find_stabilization(tk, ik, criterion)}

    destination.mkdir(parents=True, exist_ok=True)
    with open(destination / REPORT_NAME, "w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(REPORT_COLUMNS)
        for entry in summary["report_times"]:
            writer.writerow([entry[column] for column in REPORT_COLUMNS])
    if n > 1:
        summary["plot_written"] = _plot(destination / PLOT_NAME, t, v, i)
    (destination / SUMMARY_NAME).write_text(
        json.dumps(summary, indent=2, allow_nan=False), encoding="utf-8")
    return summary
