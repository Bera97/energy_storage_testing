"""Offline tests for dual-channel planning. No hardware execution."""
from copy import deepcopy
import json
from unittest.mock import patch
import pytest
import yaml
import smu
from experiments.dual_preconditioning import build_dual_plan, main
import test_experiment_config as config_tests
@pytest.fixture
def pair():
    case = config_tests.ExperimentConfigTests()
    case.setUp()
    try:
        first = deepcopy(case.document)
        first["charge"]["cv_voltage_tolerance_v"] = 0.02
        first["acquisition"]["maximum_sample_gap_s"] = 0.25
        first["instrument"]["channel"] = 1
        first["dut"]["specimen_id"] = "SPECIMEN-A"
        second = deepcopy(first)
        second["instrument"]["channel"] = 2
        second["dut"]["specimen_id"] = "SPECIMEN-B"
        yield [first, second]
    finally:
        case.doCleanups()
def test_valid_pair_is_hardware_free_and_preserves_inputs(pair):
    before = deepcopy(pair)
    with patch(
        "experiments.run_live._new_smu",
        side_effect=AssertionError("Hardware construction attempted"),
    ) as factory:
        result = build_dual_plan(pair)
    factory.assert_not_called()
    assert pair == before
    assert result["channel_count"] == 2
    assert result["execution_enabled"] is False
    assert result["live_backend_available"] is False
    assert result["will_connect"] is False
    assert result["will_enable_output"] is False
    assert result["joint_timing_qualified"] is False
def test_reversed_inputs_are_sorted_by_channel(pair):
    result = build_dual_plan(list(reversed(pair)))
    assert [item["channel"] for item in result["channels"]] == [1, 2]
@pytest.mark.parametrize("category", ["edlc", "lic"])
def test_different_device_categories_are_allowed(pair, category):
    pair[1]["dut"]["category"] = category
    pair[1]["charge"]["current"] = {
        "basis": "rated_max_charge_fraction",
        "factor": 0.25,
    }
    result = build_dual_plan(pair)
    assert result["channels"][0]["acquisition_plan"]["resolved_currents"][
        "charge_current_a"
    ] == pytest.approx(1.0)
    assert result["channels"][1]["acquisition_plan"]["resolved_currents"][
        "charge_current_a"
    ] == pytest.approx(0.5)
def test_profiles_can_have_different_cycle_counts(pair):
    pair[1]["experiment"]["cycles"] = 1
    result = build_dual_plan(pair)
    plans = [item["acquisition_plan"] for item in result["channels"]]
    assert plans[0]["phase_count"] == 8
    assert plans[1]["phase_count"] == 4
@pytest.mark.parametrize("count", [0, 1, 3])
def test_exactly_two_profiles_required(pair, count):
    documents = [deepcopy(pair[0]) for _ in range(count)]
    with pytest.raises(ValueError, match="Exactly two"):
        build_dual_plan(documents)
def test_duplicate_channels_rejected(pair):
    pair[1]["instrument"]["channel"] = 1
    with pytest.raises(ValueError, match="channel 1"):
        build_dual_plan(pair)
def test_different_resources_rejected(pair):
    pair[1]["instrument"]["visa_address"] = "OTHER::INSTR"
    with pytest.raises(ValueError, match="same exact VISA"):
        build_dual_plan(pair)
def test_duplicate_specimens_rejected(pair):
    pair[1]["dut"]["specimen_id"] = pair[0]["dut"]["specimen_id"]
    with pytest.raises(ValueError, match="different specimen"):
        build_dual_plan(pair)
def test_invalid_second_profile_is_not_ignored(pair):
    pair[1]["charge"]["target_voltage_v"] = 99
    with pytest.raises(ValueError):
        build_dual_plan(pair)
def test_missing_monitoring_rejected(pair):
    del pair[1]["acquisition"]["maximum_sample_gap_s"]
    with pytest.raises(ValueError):
        build_dual_plan(pair)
def test_no_recording_directories_created(pair, tmp_path):
    output = tmp_path / "must_not_be_created"
    for document in pair:
        document["output"]["root_directory"] = str(output)
    build_dual_plan(pair)
    assert not output.exists()
def test_public_cli_reads_both_profiles(pair, tmp_path, capsys):
    arguments = ["dual-plan"]
    for index, document in enumerate(pair):
        path = tmp_path / f"channel_{index + 1}.yaml"
        path.write_text(yaml.safe_dump(document), encoding="utf-8")
        arguments += ["--config", str(path)]
    assert smu.main(arguments) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["channel_count"] == 2
    assert result["live_backend_available"] is False
def test_execution_argument_is_rejected(capsys):
    with pytest.raises(SystemExit) as caught:
        main([
            "--config", "unused1.yaml",
            "--config", "unused2.yaml",
            "--execute",
        ])
    assert caught.value.code == 2
def test_single_config_cli_rejected(capsys):
    with pytest.raises(SystemExit) as caught:
        main(["--config", "unused.yaml"])
    assert caught.value.code == 2
