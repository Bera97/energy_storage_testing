"""Leakage analysis: integrity checks, report times, headline current, stabilization."""
import json
import pytest

import test_charge_current_sweep
from analysis.leakage_analysis import (
    PLOT_NAME, REPORT_NAME, SUMMARY_NAME, analyze_leakage_run, find_stabilization,
)
from experiments.leakage_runner import CSV_NAME, METADATA_NAME
from test_leakage_runner import Rig, early_doc, reading, small_doc

profile = test_charge_current_sweep.profile


def recorded(base, tmp_path, behavior=None, document=None):
    rig = Rig(document or small_doc(base, report_times_s=[2, 6, 10]), behavior)
    run = tmp_path / "run"
    rig.run(run)
    return rig, run


def test_small_run_is_analyzed_with_report_times(profile, tmp_path):
    rig, run = recorded(profile, tmp_path)
    result = analyze_leakage_run(run, tmp_path / "out")
    current = rig.compliance / 1000
    assert result["valid"] and result["complete"] and result["samples"] == 9
    assert result["final_window"]["samples"] == 3
    assert result["leakage_current_a"] == pytest.approx(current)
    assert result["initial_current_a"] == pytest.approx(current)
    assert [entry["nearest_sample_s"] for entry in result["report_times"]] == [2.0, 6.0, 10.0]
    assert [entry["window_samples"] for entry in result["report_times"]] == [5, 5, 3]
    assert result["in_compliance_samples"] == 0
    assert [w for w in result["warnings"] if "ranging" not in w] == []
    assert json.loads((tmp_path / "out" / SUMMARY_NAME).read_text()) == result
    assert (tmp_path / "out" / REPORT_NAME).read_text().splitlines()[0].startswith("requested_s")
    assert result["stabilization"]["source"] == "reference_criterion"
    assert result["stabilization"]["found"] is False and result["stabilization"]["checks_evaluated"] == 0


def test_tampered_csv_is_invalid(profile, tmp_path):
    rig, run = recorded(profile, tmp_path)
    with open(run / CSV_NAME, "a", encoding="utf-8") as handle:
        handle.write("\n")
    result = analyze_leakage_run(run, tmp_path / "out")
    assert result["valid"] is False
    assert result["checks"]["csv_sha256_matches"] is False
    assert result["checks"]["row_count_matches"] is True


def test_truncated_csv_is_invalid(profile, tmp_path):
    rig, run = recorded(profile, tmp_path)
    lines = (run / CSV_NAME).read_text(encoding="utf-8").splitlines(keepends=True)
    (run / CSV_NAME).write_text("".join(lines[:-1]), encoding="utf-8")
    result = analyze_leakage_run(run, tmp_path / "out")
    assert result["valid"] is False and result["checks"]["row_count_matches"] is False


def test_crashed_run_with_running_state_is_partial_but_readable(profile, tmp_path):
    rig, run = recorded(profile, tmp_path)
    meta = json.loads((run / METADATA_NAME).read_text())
    meta.update(state="running", csv_sha256=None, recorded_sample_count=0, shutdown_verified=None)
    (run / METADATA_NAME).write_text(json.dumps(meta))
    result = analyze_leakage_run(run, tmp_path / "out")
    assert result["valid"] is True and result["complete"] is False
    assert any("partial" in warning for warning in result["warnings"])


def test_compliance_samples_are_reported_and_flagged(profile, tmp_path):
    def early_compliance(rig):
        return reading(rig.hold, rig.compliance / 1000, status=2 if rig.reads <= 3 else 0)

    rig, run = recorded(profile, tmp_path, early_compliance)
    result = analyze_leakage_run(run, tmp_path / "out")
    assert result["in_compliance_samples"] == 3 and result["last_in_compliance_s"] == 1.0
    assert not any("compliance" in warning for warning in result["warnings"])

    def late_compliance(rig):
        return reading(rig.hold, rig.compliance / 1000, status=2 if rig.reads >= 8 else 0)

    rig, run = recorded(profile, tmp_path / "second", late_compliance)
    result = analyze_leakage_run(run, tmp_path / "second_out")
    assert any("compliance" in warning for warning in result["warnings"])


def test_early_stopped_run_replays_the_stop_and_marks_unreached_report_times(profile, tmp_path):
    doc = early_doc(profile)
    doc["leakage"]["report_times_s"] = [50, 120, 300]
    rig, run = recorded(profile, tmp_path, document=doc)
    result = analyze_leakage_run(run, tmp_path / "out")
    assert result["state"] == "stopped_early" and result["valid"]
    stable = result["stabilization"]
    assert stable["source"] == "configured_early_stop" and stable["found"] is True
    assert stable["stable_since_s"] == 100.0 and stable["confirmed_at_s"] == 120.0
    assert [entry["reached"] for entry in result["report_times"]] == [True, True, False]


def test_find_stabilization_detects_when_a_declining_current_levels_off():
    times = [float(t) for t in range(0, 401)]
    currents = [3.0 - 0.01 * t if t < 150 else 1.5 for t in times]
    criterion = {"minimum_duration_s": 100.0, "window_s": 20.0, "relative_change": 0.01,
                 "check_interval_s": 10.0, "consecutive_checks": 3}
    found = find_stabilization(times, currents, criterion)
    assert found["found"] and found["stable_since_s"] == 190.0 and found["confirmed_at_s"] == 210.0
    decaying = [2.718281828 ** (-t / 50) for t in times]
    assert find_stabilization(times, decaying, criterion)["found"] is False


def test_profile_mismatch_and_missing_files_are_rejected(profile, tmp_path):
    rig, run = recorded(profile, tmp_path)
    other = small_doc(profile, report_times_s=[2, 6, 10])
    other["dut"]["specimen_id"] = "different"
    with pytest.raises(ValueError, match="differs"):
        analyze_leakage_run(run, tmp_path / "out", expected_document=other)
    assert analyze_leakage_run(run, tmp_path / "ok", expected_document=rig.document)["valid"]
    with pytest.raises(OSError):
        analyze_leakage_run(tmp_path / "nothing", tmp_path / "out2")


def test_plot_is_written_when_matplotlib_is_available(profile, tmp_path):
    pytest.importorskip("matplotlib")
    rig, run = recorded(profile, tmp_path)
    result = analyze_leakage_run(run, tmp_path / "out")
    assert result["plot_written"] is True and (tmp_path / "out" / PLOT_NAME).stat().st_size > 0
