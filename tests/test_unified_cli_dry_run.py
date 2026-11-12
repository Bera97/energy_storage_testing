"""Dry-run tests: the real CLI as a subprocess, no hardware.

Recorded runs come from fake-instrument fixtures. Previews and
analysis never connect to an instrument.
"""

import subprocess
import sys
from pathlib import Path

import pytest
import yaml

import test_dual_instrument_io as io_tests
import test_dual_phase as phase_tests
import test_recorded_preconditioning_integration as integration_tests
import test_unified_cli as unified
import experiments.dual_instrument_io as io
from experiments.dual_coordinator import VirtualClock
from experiments.dual_instrument_io import DualInstrumentSession
from experiments.dual_live import execute as dual_execute
from experiments.dual_phase import DualPhase
from experiments.dual_preconditioning import build_dual_plan
from recording.preconditioning_experiment import (
    run_recorded_preconditioning,
)

REPOSITORY = Path(__file__).resolve().parents[1]
SINGLE_RUN = "20261110_220000_channel1"
DUAL_RUN = "20261110_220000_dual"


def cli(*arguments):
    return subprocess.run(
        [sys.executable, "-m", "smu", *arguments],
        cwd=REPOSITORY,
        capture_output=True,
        text=True,
    )


@pytest.fixture
def recorded(tmp_path, monkeypatch):
    case = integration_tests.RecordedPreconditioningIntegrationTests(
        methodName="test_two_cycles_complete_through_concrete_executor"
    )
    case.setUp()

    analysis = tmp_path / "analysis_outputs"
    single_root = case.fixture.recording_fixture.root
    run_recorded_preconditioning(
        case.fixture.document, case.backend, single_root, SINGLE_RUN
    )
    single_profile = tmp_path / "single.yaml"
    single_profile.write_text(
        yaml.safe_dump(
            unified.make_document(
                root=single_root, analysis_directory=analysis
            )
        ),
        encoding="utf-8",
    )

    dual_root = tmp_path / "dual_runs"
    documents = list(
        unified.make_pair(dual_root, analysis_directory=analysis)
    )
    for document in documents:
        document["experiment"]["cycles"] = 1
        document["charge"]["maximum_duration_s"] = 5
        document["charge"]["maximum_cv_duration_s"] = 3
        document["charge"]["termination"]["hold_time_s"] = 0.5
        document["discharge"]["maximum_duration_s"] = 5
        document["rest"]["after_charge_s"] = 0.05
        document["rest"]["after_discharge_s"] = 0.05
    dual_profiles = []
    for index, document in enumerate(documents):
        path = tmp_path / f"dual_{index}.yaml"
        path.write_text(yaml.safe_dump(document), encoding="utf-8")
        dual_profiles.append(str(path))

    driver = phase_tests.SourceSMU()
    driver.operating = 0
    clock = VirtualClock()
    monkeypatch.setattr(io, "SpotAcquisition", io_tests.FakeSpot)

    def phase_factory(session, channel, phase, directory, run_id, **kw):
        task = DualPhase(session, channel, phase, directory, run_id, **kw)
        driver.phases[channel] = task
        return task

    def sleep(duration):
        clock.sleep(duration)
        for channel, task in driver.phases.items():
            selected = driver.chan1 if channel == 1 else driver.chan2
            if task.state != "active" or not selected.on:
                continue
            nxt = getattr(task, "_test_next_sample", task.started + 0.2)
            while nxt <= clock.now + 1e-10:
                elapsed = nxt - task.started
                if task.phase["type"] == "charge_cc_cv":
                    sample = (task.phase["target_voltage_v"], 0.05, 1000 + nxt, 0)
                else:
                    sample = (
                        max(2.6, 3.8 - 2 * elapsed),
                        -task.phase["current_magnitude_a"],
                        1000 + nxt,
                        1,
                    )
                driver.samples[channel].append(sample)
                nxt += 0.2
            task._test_next_sample = nxt

    dual_execute(
        build_dual_plan(documents),
        dual_root / DUAL_RUN,
        "TESTSERIAL",
        clock=clock,
        sleep=sleep,
        session_factory=lambda plan, serial: DualInstrumentSession(
            plan, serial, smu_factory=lambda *a, **k: driver
        ),
        phase_factory=phase_factory,
    )

    try:
        yield {
            "single": str(single_profile),
            "dual": dual_profiles,
            "analysis": analysis,
        }
    finally:
        case.doCleanups()


@pytest.mark.parametrize("extra", [[], ["--preview"]])
def test_preview_single(recorded, extra):
    result = cli("--config", recorded["single"], *extra)
    assert result.returncode == 0, result.stderr
    assert '"will_connect": false' in result.stdout


@pytest.mark.parametrize("extra", [[], ["--preview"]])
def test_preview_dual(recorded, extra):
    result = cli(
        "--config", recorded["dual"][0],
        "--config", recorded["dual"][1],
        *extra,
    )
    assert result.returncode == 0, result.stderr
    assert '"will_connect": false' in result.stdout


def test_preview_repository_profiles():
    one = cli("--config", "configs/preconditioning.channel1.yaml")
    two = cli(
        "--config", "configs/preconditioning.channel1.yaml",
        "--config", "configs/preconditioning.channel2.yaml",
    )
    assert one.returncode == 0, one.stderr
    assert two.returncode == 0, two.stderr


def test_analyze_single_run_then_refuse_repeat(recorded):
    first = cli("--config", recorded["single"], "--analyze", SINGLE_RUN)
    assert first.returncode == 0, first.stderr
    export = recorded["analysis"] / SINGLE_RUN
    assert (export / "ANALYSIS_COMPLETE").is_file()
    assert (export / "summary.csv").is_file()

    again = cli("--config", recorded["single"], "--analyze", SINGLE_RUN)
    assert again.returncode == 1


def test_analyze_dual_batch_then_refuse_repeat(recorded):
    arguments = (
        "--config", recorded["dual"][0],
        "--config", recorded["dual"][1],
        "--analyze", DUAL_RUN,
    )
    first = cli(*arguments)
    assert first.returncode == 0, first.stderr
    export = recorded["analysis"] / DUAL_RUN
    assert (export / "ANALYSIS_COMPLETE").is_file()
    assert len(list(export.glob("channel_*_phase_*"))) == 4

    assert cli(*arguments).returncode == 1


@pytest.mark.parametrize(
    ("arguments", "code"),
    [
        (["--analyze", "nope"], 1),
        (["--analyze"], 2),
        (["--preview", "--execute"], 2),
        (["--preview", "--analyze", "x"], 2),
        (["--execute", "--analyze", "x"], 2),
    ],
)
def test_flag_errors(recorded, arguments, code):
    result = cli("--config", recorded["single"], *arguments)
    assert result.returncode == code


def test_missing_config_is_usage_error():
    assert cli("--preview").returncode == 2


def test_legacy_command_is_rejected_and_help_still_works(recorded):
    result = cli("plan", "--config", recorded["single"])
    assert result.returncode == 2
    assert "unrecognized arguments: plan" in result.stderr
    help_result = cli("--help")
    assert help_result.returncode == 0, help_result.stderr
    assert "--preview" in help_result.stdout
    assert "--execute" in help_result.stdout
    assert "--analyze" in help_result.stdout
