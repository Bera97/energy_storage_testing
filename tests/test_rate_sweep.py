"""Offline regression tests for rate-sweep planning."""
from copy import deepcopy
import json
import pytest
import yaml
import smu
from experiments.rate_sweep import build_sweep, load_sweep
PROFILE = {
    "schema_version": 1,
    "experiment": {
        "type": "preconditioning", "name": "fixture", "cycles": 1,
    },
    "dut": {
        "name": "fixture", "specimen_id": "TEST", "category": "edlc",
        "chemistry": "EDLC", "manufacturer": "fixture", "model": "fixture",
        "nominal_capacity_ah": 0.001,
        "ratings": {
            "minimum_voltage_v": 0,
            "maximum_voltage_v": 5.55,
            "maximum_continuous_charge_current_a": 0.1,
            "maximum_continuous_discharge_current_a": 0.1,
        },
    },
    "instrument": {
        "visa_address": "TEST", "channel": 1,
        "four_wire": False, "nplc": 1,
    },
    "acquisition": {
        "interval_s": 0.2, "maximum_sample_gap_s": 0.5,
    },
    "charge": {
        "mode": "cc_cv",
        "current": {"c_rate": 2},
        "target_voltage_v": 5.5,
        "cv_voltage_tolerance_v": 0.05,
        "termination": {
            "current_reference": "configured_charge_current",
            "current_factor": 0.05,
            "hold_time_s": 5,
        },
        "maximum_duration_s": 19800,
        "maximum_cv_duration_s": 1800,
    },
    "rest": {"after_charge_s": 15, "after_discharge_s": 15},
    "discharge": {
        "mode": "constant_current",
        "current": {
            "basis": "rated_max_discharge_fraction", "factor": 0.1,
        },
        "cutoff_voltage_v": 0.02,
        "maximum_duration_s": 3600,
    },
    "output": {"root_directory": "unused_test_runs"},
}
SETTINGS = {
    "schema_version": 1,
    "kind": "charging",
    "values": [0.2, 0.5, 1],
    "repetitions": 3,
    "reference": {"discharge_fraction": 0.5},
    "rest_after_charge_s": 900,
    "rest_after_discharge_s": 900,
    "charge_maximum_duration_s": 19800,
    "discharge_maximum_duration_s": 3600,
}
def test_charge_sweep_preserves_reference_and_inputs():
    profile, settings = deepcopy(PROFILE), deepcopy(SETTINGS)
    plan = build_sweep(settings, profile)
    assert profile == PROFILE and settings == SETTINGS
    assert plan["total_cycles"] == 9
    assert plan["total_phases"] == 36
    assert [
        point["charge_current_a"] for point in plan["points"]
    ] == pytest.approx([0.0002, 0.0005, 0.001])
    assert all(
        point["discharge_current_a"] == pytest.approx(0.05)
        for point in plan["points"]
    )
    assert all(
        point["largest_phase_record_count"] == 99001
        for point in plan["points"]
    )
    for point in plan["points"]:
        phases = point["acquisition_plan"]["phases"]
        assert [phase["type"] for phase in phases] == [
            "charge_cc_cv", "rest", "discharge_cc", "rest",
        ] * 3
        assert all(
            phase["duration_s"] == 900
            for phase in phases if phase["type"] == "rest"
        )
    assert plan["will_connect"] is False
    assert plan["hardware_execution_available"] is False
def test_discharge_sweep_holds_charge_fixed():
    settings = {
        **deepcopy(SETTINGS),
        "kind": "discharging",
        "values": [0.1, 0.25, 0.5, 0.75, 1],
        "reference": {"charge_current": {"c_rate": 2}},
    }
    points = build_sweep(settings, PROFILE)["points"]
    assert all(
        point["charge_current_a"] == pytest.approx(0.002)
        for point in points
    )
    assert [
        point["discharge_current_a"] for point in points
    ] == pytest.approx([0.01, 0.025, 0.05, 0.075, 0.1])
@pytest.mark.parametrize(
    "values",
    [[0], [-1], [True], [float("nan")], [float("inf")], [1, 1], []],
)
def test_invalid_rates(values):
    with pytest.raises(ValueError):
        build_sweep({**SETTINGS, "values": values}, PROFILE)
def test_finite_buffer_limit_is_not_bypassed():
    with pytest.raises(ValueError, match="planning limit"):
        build_sweep({
            **SETTINGS, "charge_maximum_duration_s": 25200,
        }, PROFILE)
def test_dut_current_limit_is_not_clamped():
    with pytest.raises(ValueError):
        build_sweep({**SETTINGS, "values": [1000]}, PROFILE)
def test_execution_ceiling_is_checked():
    profile = deepcopy(PROFILE)
    profile["execution"] = {
        "expected_serial": "TEST",
        "maximum_voltage_v": 5.55,
        "maximum_current_a": 0.001,
    }
    with pytest.raises(ValueError, match="execution ceilings"):
        build_sweep(SETTINGS, profile)
def test_strict_yaml_relative_profile_and_cli(tmp_path, capsys):
    folder = tmp_path / "profiles"
    folder.mkdir()
    (folder / "base.yaml").write_text(
        yaml.safe_dump(PROFILE), encoding="utf-8",
    )
    path = folder / "sweep.yaml"
    path.write_text(
        yaml.safe_dump({
            "profile": "base.yaml", "sweep": SETTINGS,
        }),
        encoding="utf-8",
    )
    assert load_sweep(path)["total_cycles"] == 9
    assert smu.main([
        "sweep-plan", "--config", str(path), "--json",
    ]) == 0
    assert json.loads(
        capsys.readouterr().out
    )["will_enable_output"] is False
    path.write_text(
        "profile: base.yaml\nprofile: base.yaml\nsweep: {}\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="Duplicate"):
        load_sweep(path)
def test_no_live_option_exists():
    with pytest.raises(SystemExit) as caught:
        smu.main([
            "sweep-plan", "--config", "unused", "--execute",
        ])
    assert caught.value.code == 2
