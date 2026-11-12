"""Offline tests for the flag-based single-command interface."""

from copy import deepcopy
import json
from pathlib import Path
import re

import pytest
import yaml

import smu
import experiments.dual_live as dual
import experiments.run_live as single


def make_document(
    *,
    root,
    file_name="channel_test",
    analysis_directory=None,
    channel=1,
    specimen_id="SPECIMEN-A",
    cycles=2,
):
    return {
        "schema_version": 1,
        "experiment": {
            "type": "preconditioning",
            "name": "synthetic_preconditioning",
            "cycles": cycles,
        },
        "dut": {
            "name": "Synthetic cell",
            "specimen_id": specimen_id,
            "category": "battery",
            "chemistry": "synthetic",
            "manufacturer": "test",
            "model": "test",
            "nominal_capacity_ah": 2.0,
            "ratings": {
                "minimum_voltage_v": 2.5,
                "maximum_voltage_v": 4.2,
                "maximum_continuous_charge_current_a": 2.0,
                "maximum_continuous_discharge_current_a": 4.0,
            },
        },
        "instrument": {
            "visa_address": "TEST::INSTR",
            "channel": channel,
            "four_wire": False,
            "nplc": 1.0,
        },
        "acquisition": {
            "interval_s": 0.2,
            "maximum_sample_gap_s": 0.25,
        },
        "charge": {
            "mode": "cc_cv",
            "current": {
                "basis": "capacity_c_rate",
                "factor": 0.5,
            },
            "target_voltage_v": 4.1,
            "cv_voltage_tolerance_v": 0.02,
            "termination": {
                "current_reference": "rated_max_charge_current",
                "current_factor": 0.05,
                "hold_time_s": 10.0,
            },
            "maximum_duration_s": 14400.0,
            "maximum_cv_duration_s": 3600.0,
        },
        "rest": {
            "after_charge_s": 900.0,
            "after_discharge_s": 900.0,
        },
        "discharge": {
            "mode": "constant_current",
            "current": {
                "basis": "rated_max_discharge_fraction",
                "factor": 0.5,
            },
            "cutoff_voltage_v": 2.7,
            "maximum_duration_s": 14400.0,
        },
        "output": {
            "root_directory": str(root),
            "file_name": file_name,
            **(
                {"analysis_directory": str(analysis_directory)}
                if analysis_directory is not None
                else {}
            ),
        },
        "execution": {
            "expected_serial": "TESTSERIAL",
            "maximum_voltage_v": 4.2,
            "maximum_current_a": 4.0,
        },
    }


def save(document, path):
    path.write_text(
        yaml.safe_dump(document),
        encoding="utf-8",
    )
    return str(path)


def make_pair(root, analysis_directory=None):
    first = make_document(
        root=root,
        channel=1,
        analysis_directory=analysis_directory,
    )
    second = deepcopy(first)
    second["instrument"]["channel"] = 2
    second["dut"]["specimen_id"] = "SPECIMEN-B"
    return first, second


def save_pair(documents, tmp_path):
    paths = []
    for index, document in enumerate(documents):
        paths.append(
            save(document, tmp_path / f"profile_{index}.yaml")
        )
    return paths


def test_preview_single_channel(tmp_path, capsys):
    path = save(make_document(root=tmp_path), tmp_path / "profile.yaml")

    assert smu.main(["--config", path]) == 0

    preview = json.loads(capsys.readouterr().out)
    assert preview["will_connect"] is False
    assert preview["will_enable_output"] is False
    assert preview["specimen_id"] == "SPECIMEN-A"


def test_preview_flag_matches_default(tmp_path, capsys):
    path = save(make_document(root=tmp_path), tmp_path / "profile.yaml")

    assert smu.main(["--config", path, "--preview"]) == 0

    preview = json.loads(capsys.readouterr().out)
    assert preview["will_connect"] is False


def test_preview_dual_channel(tmp_path, capsys):
    paths = save_pair(make_pair(tmp_path), tmp_path)

    assert smu.main(["--config", paths[0], "--config", paths[1]]) == 0

    plan = json.loads(capsys.readouterr().out)
    assert plan["channel_count"] == 2
    assert plan["will_connect"] is False


def test_execute_uses_timestamped_run_folder(
    tmp_path, monkeypatch, capsys
):
    document = make_document(root=tmp_path)
    path = save(document, tmp_path / "profile.yaml")
    calls = []

    def fake(document_argument, **kwargs):
        calls.append(kwargs)

        (tmp_path / kwargs["experiment_id"]).mkdir()

        return (
            {"completed_phase_count": 4, "shutdown_verified": True},
            tmp_path / kwargs["experiment_id"],
        )

    monkeypatch.setattr(single, "execute_live", fake)

    assert smu.main(["--config", path, "--execute"]) == 0

    experiment_id = calls[0]["experiment_id"]
    assert re.fullmatch(
        r"\d{8}_\d{6}_channel_test",
        experiment_id,
    )
    assert (tmp_path / experiment_id).is_dir()
    assert json.loads(capsys.readouterr().out)["state"] == "completed"


def test_execute_without_file_name_is_refused(
    tmp_path, monkeypatch, capsys
):
    document = make_document(root=tmp_path)
    del document["output"]["file_name"]
    path = save(document, tmp_path / "profile.yaml")

    def forbidden(*args, **kwargs):
        pytest.fail("Execution attempted without output.file_name")

    monkeypatch.setattr(single, "execute_live", forbidden)

    assert smu.main(["--config", path, "--execute"]) == 1
    assert "output.file_name" in capsys.readouterr().err


def test_execute_with_placeholder_file_name_is_refused(
    tmp_path, monkeypatch, capsys
):
    document = make_document(root=tmp_path)
    document["output"]["file_name"] = "REPLACE_WITH_A_NAME"
    path = save(document, tmp_path / "profile.yaml")

    def forbidden(*args, **kwargs):
        pytest.fail("Execution attempted with a placeholder name")

    monkeypatch.setattr(single, "execute_live", forbidden)

    assert smu.main(["--config", path, "--execute"]) == 1
    assert "output.file_name" in capsys.readouterr().err


def test_failed_run_skips_analysis(tmp_path, monkeypatch, capsys):
    path = save(make_document(root=tmp_path), tmp_path / "profile.yaml")

    def failing(document_argument, **kwargs):
        raise RuntimeError("synthetic execution failure")

    monkeypatch.setattr(single, "execute_live", failing)

    status = smu.main(
        ["--config", path, "--execute", "--analyze"]
    )

    assert status == 1
    captured = capsys.readouterr()
    assert "analysis skipped" in captured.err


def test_invalid_flag_combinations(tmp_path):
    path = save(make_document(root=tmp_path), tmp_path / "profile.yaml")

    with pytest.raises(SystemExit) as caught:
        smu.main(["--config", path, "--preview", "--execute"])
    assert caught.value.code == 2

    with pytest.raises(SystemExit) as caught:
        smu.main(["--config", path, "--preview", "--analyze", "run"])
    assert caught.value.code == 2

    with pytest.raises(SystemExit) as caught:
        smu.main(["--config", path, "--execute", "--analyze", "run"])
    assert caught.value.code == 2

    with pytest.raises(SystemExit) as caught:
        smu.main(["--config", path, "--analyze"])
    assert caught.value.code == 2


def test_analyze_unknown_run_reports_error(tmp_path, capsys):
    path = save(
        make_document(
            root=tmp_path,
            analysis_directory=tmp_path / "analysis_outputs",
        ),
        tmp_path / "profile.yaml",
    )

    assert smu.main(["--config", path, "--analyze", "missing"]) == 1
    assert "missing" in capsys.readouterr().err


def test_analyze_without_analysis_directory_is_refused(
    tmp_path, capsys
):
    document = make_document(root=tmp_path)
    path = save(document, tmp_path / "profile.yaml")
    (tmp_path / "recorded_run").mkdir()
    (tmp_path / "recorded_run" / "experiment.json").write_text(
        "{}",
        encoding="utf-8",
    )

    assert smu.main(["--config", path, "--analyze", "recorded_run"]) == 1
    assert "analysis_directory" in capsys.readouterr().err


def test_analyze_uses_yaml_directories(
    tmp_path, monkeypatch, capsys
):
    root = tmp_path / "runs"
    analysis_root = tmp_path / "analysis_outputs"
    run_name = "20260101_120000_channel_test"
    run_directory = root / run_name
    run_directory.mkdir(parents=True)
    (run_directory / "experiment.json").write_text("{}", encoding="utf-8")
    path = save(
        make_document(root=root, analysis_directory=analysis_root),
        tmp_path / "profile.yaml",
    )
    forwarded = []

    def fake_analyze_main(argv):
        forwarded.append(list(argv))
        return 0

    monkeypatch.setattr(smu, "_analyze_main", fake_analyze_main)

    assert smu.main(["--config", path, "--analyze", run_name]) == 0

    assert forwarded == [
        [
            str(run_directory),
            str(analysis_root / run_name),
        ]
    ]
    assert analysis_root.is_dir()


def test_dual_execute_uses_timestamped_batch_folder(
    tmp_path, monkeypatch, capsys
):
    paths = save_pair(make_pair(tmp_path), tmp_path)
    calls = []

    def fake(plan, directory, serial):
        calls.append((plan, directory, serial))
        Path(directory).mkdir()
        return {
            "state": "completed",
            "final_stop_verified": True,
            "session_close_returned": True,
        }

    monkeypatch.setattr(dual, "execute", fake)

    assert smu.main([
        "--config", paths[0],
        "--config", paths[1],
        "--execute",
    ]) == 0

    batch_directory = calls[0][1]
    assert re.fullmatch(
        r".*[\\/]?\d{8}_\d{6}_channel_test$",
        str(batch_directory),
    )
    assert batch_directory.is_dir()
    assert json.loads(capsys.readouterr().out)["state"] == "completed"


def test_unified_help_lists_three_flags(capsys):
    assert smu.main(["--help"]) == 0
    output = capsys.readouterr().out
    assert "--preview" in output
    assert "--execute" in output
    assert "--analyze" in output


def test_legacy_commands_are_rejected(tmp_path, capsys):
    path = save(make_document(root=tmp_path), tmp_path / "profile.yaml")
    with pytest.raises(SystemExit) as caught:
        smu.main(["plan", "--config", path])
    assert caught.value.code == 2
    assert "usage:" in capsys.readouterr().err


def test_execute_then_analyze_end_to_end(tmp_path, monkeypatch, capsys):
    import test_recorded_preconditioning_integration as integration_tests
    from recording.preconditioning_experiment import (
        run_recorded_preconditioning,
    )

    case = integration_tests.RecordedPreconditioningIntegrationTests(
        methodName="test_two_cycles_complete_through_concrete_executor"
    )
    case.setUp()

    try:
        root = case.fixture.recording_fixture.root
        analysis_root = tmp_path / "analysis_outputs"
        path = save(
            make_document(
                root=root,
                analysis_directory=analysis_root,
            ),
            tmp_path / "profile.yaml",
        )

        def fake(document_argument, *, experiment_id, **kwargs):
            return run_recorded_preconditioning(
                case.fixture.document,
                case.backend,
                root,
                experiment_id,
            )

        monkeypatch.setattr(single, "execute_live", fake)

        assert smu.main([
            "--config", path,
            "--execute",
            "--analyze",
        ]) == 0

        recorded = [
            item
            for item in root.iterdir()
            if item.is_dir()
            and re.fullmatch(
                r"\d{8}_\d{6}_channel_test",
                item.name,
            )
        ]
        assert len(recorded) == 1

        destination = analysis_root / recorded[0].name

        assert (destination / "ANALYSIS_COMPLETE").is_file()
        assert (destination / "summary.csv").is_file()
        assert (destination / "summary.json").is_file()

        captured = capsys.readouterr()
        assert "Analysis:" in captured.out
    finally:
        case.doCleanups()


def test_analyze_recorded_dual_batch(tmp_path, monkeypatch, capsys):
    import experiments.dual_instrument_io as io
    from experiments.dual_coordinator import VirtualClock
    from experiments.dual_instrument_io import DualInstrumentSession
    from experiments.dual_phase import DualPhase
    from experiments.dual_preconditioning import build_dual_plan
    import test_dual_instrument_io as io_tests
    import test_dual_phase as phase_tests
    from experiments.dual_live import execute as rig_execute

    documents = list(
        make_pair(
            tmp_path,
            analysis_directory=tmp_path / "analysis_outputs",
        )
    )

    for document in documents:
        document["experiment"]["cycles"] = 1
        document["charge"]["maximum_duration_s"] = 5
        document["charge"]["maximum_cv_duration_s"] = 3
        document["charge"]["termination"]["hold_time_s"] = 0.5
        document["discharge"]["maximum_duration_s"] = 5
        document["rest"]["after_charge_s"] = 0.05
        document["rest"]["after_discharge_s"] = 0.05

    analysis_root = tmp_path / "analysis_outputs"
    batch_name = "20260101_120000_channel_test"
    paths = save_pair(documents, tmp_path)
    plan = build_dual_plan(documents)
    driver = phase_tests.SourceSMU()
    driver.operating = 0
    clock = VirtualClock()

    monkeypatch.setattr(io, "SpotAcquisition", io_tests.FakeSpot)

    def session_factory(plan, serial):
        return DualInstrumentSession(
            plan,
            serial,
            smu_factory=lambda *args, **kwargs: driver,
        )

    def phase_factory(
        session, channel, phase, directory, run_id, **kwargs
    ):
        task = DualPhase(
            session, channel, phase, directory, run_id, **kwargs
        )
        driver.phases[channel] = task
        return task

    def sleep(duration):
        clock.sleep(duration)
        for channel, task in driver.phases.items():
            selected = driver.chan1 if channel == 1 else driver.chan2
            if task.state != "active" or not selected.on:
                continue
            next_sample = getattr(
                task, "_test_next_sample", task.started + 0.2
            )
            while next_sample <= clock.now + 1e-10:
                elapsed = next_sample - task.started
                if task.phase["type"] == "charge_cc_cv":
                    voltage = task.phase["target_voltage_v"]
                    current = 0.05
                    status = 0
                else:
                    voltage = max(2.6, 3.8 - 2 * elapsed)
                    current = -task.phase["current_magnitude_a"]
                    status = 1
                driver.samples[channel].append(
                    (voltage, current, 1000 + next_sample, status)
                )
                next_sample += 0.2
            task._test_next_sample = next_sample

    options = dict(
        clock=clock,
        sleep=sleep,
        session_factory=session_factory,
        phase_factory=phase_factory,
    )

    assert rig_execute(
        plan,
        tmp_path / batch_name,
        "TESTSERIAL",
        **options,
    )["state"] == "completed"

    assert smu.main([
        "--config", paths[0],
        "--config", paths[1],
        "--analyze", batch_name,
    ]) == 0

    destination = analysis_root / batch_name

    assert (destination / "ANALYSIS_COMPLETE").is_file()
    assert (destination / "summary.json").is_file()
    assert (destination / "summary.csv").is_file()
    assert list(destination.glob("channel_*_phase_*"))

    captured = capsys.readouterr()
    assert "Analysis:" in captured.out
