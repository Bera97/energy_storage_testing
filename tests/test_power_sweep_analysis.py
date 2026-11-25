"""Offline energy/export tests using native fake-driver sweep recordings."""
import csv
import json
import shutil
import pytest
from test_recorded_power_sweep import context, small
from analysis.analyze_power_sweep import analyze_power_sweep, _integrate, _ratio
from analysis.export_power_sweep import export_power_sweep_analysis, validate_power_sweep_analysis
from experiments.power_sweep_contract import PowerSweepError, write_json, digest


def source(context, tmp_path, measured=False):
    small(context, measured=measured)
    context[2]()
    return tmp_path / "sweep"


def file_snapshot(root):
    return {str(p.relative_to(root)):p.read_bytes() for p in root.rglob("*") if p.is_file()}


def test_analysis_is_read_only_and_keeps_preparation_out_of_cycle_results(context, tmp_path):
    root = source(context, tmp_path)
    before = file_snapshot(root)
    result = analyze_power_sweep(root)
    assert file_snapshot(root) == before
    assert len(result["phases"]) == 6 and len(result["cycles"]) == 1 and len(result["conditions"]) == 1
    assert result["phases"][0]["scope"] == "initial_preparation"
    assert result["cycles"][0]["charge_stage_id"] == "stage_00002"
    assert result["cycles"][0]["discharge_stage_id"] == "stage_00004"
    assert result["physical_qualification"] is False


def test_cp_uses_host_midpoints_excludes_probe_and_does_not_require_instrument_time_continuity(context, tmp_path):
    result = analyze_power_sweep(source(context, tmp_path))
    phase = next(p for p in result["phases"] if p["type"] == "discharge_cp")
    assert phase["time_basis"] == "host_read_bracket_midpoint_estimate"
    assert phase["excluded_probe_count"] == 1 and phase["sample_count"] == 3 and phase["interval_count"] == 2
    assert phase["recorded_span_s"] == pytest.approx(2.0, abs=0.002)
    assert phase["signed_energy_wh"] < 0 and phase["negative_energy_wh"] > 0
    assert phase["positive_energy_wh"] == 0.0


def test_unmeasured_rest_has_null_totals_not_zero(context, tmp_path):
    result = analyze_power_sweep(source(context, tmp_path))
    for phase in result["phases"]:
        if phase["type"] == "rest":
            assert phase["sample_count"] == 0 and phase["signed_energy_wh"] is None
            assert phase["recorded_span_s"] is None and not phase["has_integration_interval"]


def test_measured_rest_is_separate_from_cycle_ratios(context, tmp_path):
    result = analyze_power_sweep(source(context, tmp_path, measured=True))
    rests = [p for p in result["phases"] if p["type"] == "rest"]
    assert len(rests) == 3 and all(p["sample_count"] >= 2 for p in rests)
    assert all(p["signed_energy_wh"] == 0.0 for p in rests)
    assert len(result["cycles"]) == 1
    assert all(p["time_basis"] == "relative_host_read_bracket_midpoint_estimate" for p in rests)


def test_analytic_irregular_interval_integral_and_no_extrapolation():
    points = [{"time_s": t, "voltage_v": 2.0, "current_a": -0.5} for t in (10.0, 11.0, 13.0)]
    result = _integrate(points)
    assert result["summary"]["recorded_span_s"] == 3.0
    assert result["summary"]["negative_energy_wh"] == pytest.approx(3/3600)
    assert result["summary"]["negative_charge_mah"] == pytest.approx(1.5/3.6)
    assert "first_instrument_timestamp_s" not in result["summary"]
    assert _ratio(2.0, 1.0) == 2.0 and _ratio(1.0, 0.0) is None and _ratio(None, 1.0) is None


def test_one_point_is_no_integration_interval():
    result = _integrate([{"time_s": 5.0, "voltage_v": 2.0, "current_a": -0.5}])
    assert result["summary"]["interval_count"] == 0
    assert result["summary"]["time_weighted_mean_signed_power_w"] is None
    assert _integrate([]) is None


def test_full_condition_repeat_cycle_order_and_statistics(context, tmp_path):
    context[2]()
    result = analyze_power_sweep(tmp_path / "sweep")
    assert len(result["cycles"]) == 8
    assert [c["power_w"] for c in result["cycles"]] == [0.8]*4+[0.4]*4
    assert [c["power_w"] for c in result["conditions"]] == [0.8, 0.4]
    assert all(c["cycle_count"] == 4 and c["repeat_count"] == 2 for c in result["conditions"])
    assert all(c["discharge_energy_wh_sample_std"] is not None for c in result["conditions"])


def test_export_is_valid_and_does_not_modify_source(context, tmp_path):
    root = source(context, tmp_path)
    before = file_snapshot(root)
    destination = tmp_path / "analysis"
    result = export_power_sweep_analysis(root, destination)
    assert result["state"] == "completed" and file_snapshot(root) == before
    checked = validate_power_sweep_analysis(destination)
    assert checked["valid"], checked["errors"]
    with (destination / "phase_samples/stage_00004.csv").open(newline="") as stream:
        rows = list(csv.DictReader(stream))
    assert [r["record_index"] for r in rows] == ["1", "2", "3"]
    assert {r["raw_instrument_timestamp_s"] for r in rows} == {"0.0"}
    assert (destination / "POWER_ANALYSIS_COMPLETE").is_file() and not (destination / "COMPLETE").exists()


def test_measured_rest_export_and_portable_derived_validation(context, tmp_path):
    root = source(context, tmp_path, measured=True)
    destination = tmp_path / "analysis"
    export_power_sweep_analysis(root, destination)
    relocated = tmp_path / "relocated_analysis"
    shutil.copytree(destination, relocated)
    shutil.rmtree(root)
    checked = validate_power_sweep_analysis(relocated)
    assert checked["valid"], checked["errors"]


@pytest.mark.parametrize("kind", ["missing_marker", "corrupt_csv", "failed_state"])
def test_invalid_source_refused_without_creating_output(context, tmp_path, kind):
    root = source(context, tmp_path)
    if kind == "missing_marker":
        (root / "POWER_SWEEP_COMPLETE").unlink()
    elif kind == "corrupt_csv":
        p = root / "stage_00004/power/power_samples.csv"
        p.write_bytes(p.read_bytes()+b"bad\n")
    else:
        p = root / "power_sweep.json"
        doc = json.loads(p.read_text());doc["state"] = "raised"
        write_json(p, doc)
    with pytest.raises(PowerSweepError):
        export_power_sweep_analysis(root, tmp_path / "analysis")
    assert not (tmp_path / "analysis").exists()


def test_existing_destination_refused(context, tmp_path):
    root = source(context, tmp_path)
    (tmp_path / "analysis").mkdir()
    with pytest.raises(FileExistsError):
        export_power_sweep_analysis(root, tmp_path / "analysis")


def test_destination_inside_source_refused(context, tmp_path):
    root = source(context, tmp_path)
    with pytest.raises(PowerSweepError):
        export_power_sweep_analysis(root, root / "analysis")
    assert not (root / "analysis").exists()


@pytest.mark.parametrize("name", ["analysis.json", "POWER_ANALYSIS_COMPLETE", "phase_summary.csv", "phase_samples/stage_00004.csv"])
def test_missing_derived_file_invalid(context, tmp_path, name):
    root = source(context, tmp_path)
    destination = tmp_path / "analysis"
    export_power_sweep_analysis(root, destination)
    (destination / name).unlink()
    assert not validate_power_sweep_analysis(destination)["valid"]


def test_csv_write_failure_retains_partial_output_without_marker(context, tmp_path, monkeypatch):
    import analysis.export_power_sweep as module
    root = source(context, tmp_path)
    def failed(*args, **kwargs):
        raise OSError("injected CSV write")
    monkeypatch.setattr(module, "_write_text", failed)
    with pytest.raises(OSError, match="CSV write"):
        export_power_sweep_analysis(root, tmp_path / "analysis")
    doc = json.loads((tmp_path / "analysis/analysis.json").read_text())
    assert doc["state"] == "raised" and not (tmp_path / "analysis/POWER_ANALYSIS_COMPLETE").exists()
    assert doc["source_snapshot"]["directory"] == str(root)
    assert "power_sweep.json" in doc["source_snapshot"]["files"]


def test_source_change_during_export_is_detected(context, tmp_path, monkeypatch):
    import analysis.export_power_sweep as module
    root = source(context, tmp_path)
    normal = module._write_text
    changed = False
    def changing(path, text):
        nonlocal changed
        normal(path, text)
        if not changed:
            p = root / "stage_00004/power/power_samples.csv"
            p.write_bytes(p.read_bytes()+b"\n")
            changed = True
    monkeypatch.setattr(module, "_write_text", changing)
    with pytest.raises(PowerSweepError, match="Source changed"):
        export_power_sweep_analysis(root, tmp_path / "analysis")
    assert not (tmp_path / "analysis/POWER_ANALYSIS_COMPLETE").exists()


def test_rebound_derived_total_does_not_bypass_recalculation(context, tmp_path):
    root = source(context, tmp_path)
    destination = tmp_path / "analysis"
    export_power_sweep_analysis(root, destination)
    p = destination / "analysis.json"
    header = json.loads(p.read_text());header["report"]["phases"][4]["negative_energy_wh"] = 10.0
    write_json(p, header)
    (destination / "POWER_ANALYSIS_COMPLETE").write_text("POWER_ANALYSIS_COMPLETE sha256="+digest(p)+"\n", newline="\n")
    checked = validate_power_sweep_analysis(destination)
    assert not checked["valid"] and "phase total" in ";".join(checked["errors"])



@pytest.mark.parametrize("loaded_count", [0, 1])
def test_probe_only_or_single_loaded_point_is_not_measured_zero(context, tmp_path, loaded_count):
    small(context)
    backend = context[1]()
    normal = backend.open_session
    def opening(request, config):
        smu = normal(request, config)
        if request["phase"]["type"] == "discharge_cp":
            query = smu._query
            def early_cutoff(command):
                value = query(command)
                if command.startswith(":MEAS?"):
                    parts = value.split(",")
                    parts[0] = "2.7" if smu.read_count > loaded_count else "3.8"
                    return ",".join(parts)
                return value
            smu._query = early_cutoff
        return smu
    backend.open_session = opening
    context[2](backend)
    root = tmp_path / "sweep"
    report = analyze_power_sweep(root)
    phase = report["phases"][4]
    assert phase["sample_count"] == loaded_count and phase["excluded_probe_count"] == 1
    assert phase["negative_energy_wh"] is None and phase["signed_charge_mah"] is None
    assert report["cycles"][0]["observed_discharge_to_charge_energy_ratio"] is None
    assert report["conditions"][0]["observed_discharge_cycle_count"] == 0
    export_power_sweep_analysis(root, tmp_path / "analysis")
    assert validate_power_sweep_analysis(tmp_path / "analysis")["valid"]


def test_cc_integral_stops_at_trigger_and_excludes_retained_tail(context, tmp_path):
    root = source(context, tmp_path)
    result = analyze_power_sweep(root)
    for phase in result["phases"]:
        if phase["type"] not in ("charge_cc_cv", "discharge_cc"):
            continue
        directory = root / phase["stage_id"] / f"phase_phase_{phase['phase_index']}"
        meta = json.loads((directory / "metadata.json").read_text())
        with (directory / "measurements.csv").open(newline="") as stream:
            rows = list(csv.DictReader(stream))
        trigger = meta["outcome"]["triggering_record_index"]
        assert phase["sample_count"] == trigger+1
        assert phase["excluded_trailing_cc_count"] == len(rows)-trigger-1
        assert phase["last_sample_time_s"] == float(rows[trigger]["instrument_timestamp_s"])


def test_analysis_and_export_never_construct_a_sweep_backend(context, tmp_path, monkeypatch):
    import experiments.power_sweep_backend as module
    root = source(context, tmp_path)
    def forbidden(*args, **kwargs):
        raise AssertionError("Offline analysis must not construct a backend")
    monkeypatch.setattr(module.RecordedPowerSweepBackend, "__init__", forbidden)
    assert analyze_power_sweep(root)["cycles"]
    export_power_sweep_analysis(root, tmp_path / "analysis")


def test_rebound_simulation_origin_is_not_accepted(context, tmp_path):
    from recording.power_phase import marker_text
    from test_recorded_power_sweep import rebind
    root = source(context, tmp_path)
    path = root / "stage_00004/power/power_phase.json"
    doc = json.loads(path.read_text());doc["simulation_only"] = True
    write_json(path, doc)
    (path.parent / "POWER_PHASE_COMPLETE").write_text(marker_text(path.read_bytes()), newline="\n")
    rebind(root, "stage_00004")
    with pytest.raises(PowerSweepError, match="origin"):
        export_power_sweep_analysis(root, tmp_path / "analysis")
    assert not (tmp_path / "analysis").exists()


def test_original_exception_survives_failed_export_failure_metadata(context, tmp_path, monkeypatch):
    import analysis.export_power_sweep as module
    root = source(context, tmp_path)
    original = OSError("primary CSV failure")
    normal = module.write_json
    def fail_csv(*args, **kwargs):
        raise original
    def fail_metadata(path, doc, **kwargs):
        if doc["state"] == "raised":
            raise OSError("secondary failure metadata")
        return normal(path, doc, **kwargs)
    monkeypatch.setattr(module, "_write_text", fail_csv)
    monkeypatch.setattr(module, "write_json", fail_metadata)
    with pytest.raises(OSError) as caught:
        export_power_sweep_analysis(root, tmp_path / "analysis")
    assert caught.value is original
    assert any("secondary failure metadata" in note for note in original.__notes__)
    assert not (tmp_path / "analysis/POWER_ANALYSIS_COMPLETE").exists()


def test_final_derived_validation_failure_removes_marker(context, tmp_path, monkeypatch):
    import analysis.export_power_sweep as module
    root = source(context, tmp_path)
    monkeypatch.setattr(module, "validate_power_sweep_analysis", lambda path: {"valid": False, "errors": ["injected"]})
    with pytest.raises(PowerSweepError, match="Derived export validation"):
        export_power_sweep_analysis(root, tmp_path / "analysis")
    assert not (tmp_path / "analysis/POWER_ANALYSIS_COMPLETE").exists()
    assert json.loads((tmp_path / "analysis/analysis.json").read_text())["state"] == "raised"


def test_source_hash_inventory_mutation_is_rejected_in_derived_export(context, tmp_path):
    root = source(context, tmp_path)
    destination = tmp_path / "analysis"
    export_power_sweep_analysis(root, destination)
    path = destination / "analysis.json"
    doc = json.loads(path.read_text());doc["report"]["source_files"].pop("power_sweep.json")
    write_json(path, doc)
    (destination / "POWER_ANALYSIS_COMPLETE").write_text("POWER_ANALYSIS_COMPLETE sha256="+digest(path)+"\n", newline="\n")
    assert not validate_power_sweep_analysis(destination)["valid"]


def test_public_cp_analysis_remains_blocked(context, tmp_path, capsys):
    import smu
    import yaml
    root = source(context, tmp_path)
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(context[0]))
    before = file_snapshot(root)
    assert smu.main(["--config", str(path), "--analyze", str(root)]) == 1
    assert "planning only" in capsys.readouterr().err.lower()
    assert file_snapshot(root) == before


def test_export_keyboard_interrupt_is_preserved_and_never_completed(context, tmp_path, monkeypatch):
    import analysis.export_power_sweep as module
    root = source(context, tmp_path)
    original = KeyboardInterrupt("injected export interrupt")
    def interrupt(*args, **kwargs):
        raise original
    monkeypatch.setattr(module, "_write_text", interrupt)
    with pytest.raises(KeyboardInterrupt) as caught:
        export_power_sweep_analysis(root, tmp_path / "analysis")
    assert caught.value is original
    assert json.loads((tmp_path / "analysis/analysis.json").read_text())["state"] == "raised"
    assert not (tmp_path / "analysis/POWER_ANALYSIS_COMPLETE").exists()
