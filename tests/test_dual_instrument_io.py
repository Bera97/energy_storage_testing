"""Offline tests for dual-channel session primitives."""
import json
import pytest
import yaml
from experiments.dual_preconditioning import build_dual_plan
import experiments.dual_instrument_io as io
import test_dual_preconditioning as planning_tests
pair = planning_tests.pair
class FakeChannel:
    def __init__(self):
        self.on = True
        self.auto = True
        self.nplc = 1.0
        self.fail_auto = False
        self.force_on_readback = False
    def set_auto_output_on(self, value):
        if self.fail_auto:
            raise OSError("synthetic automatic-output command failure")
        self.auto = value
    def disable_output(self):
        self.on = False
    def get_output_state(self):
        return True if self.force_on_readback else self.on
    def get_auto_output_on(self):
        return self.auto
    def get_nplc(self):
        return self.nplc
class FakeSMU:
    def __init__(self):
        self.chan1 = FakeChannel()
        self.chan2 = FakeChannel()
        self.identity = "Keysight,B2902B,TESTSERIAL,FW"
        self.is_connected = False
        self.commands = []
        self.shutdown_calls = 0
        self.closed = False
    def connect(self):
        self.is_connected = True
    def _write(self, command):
        self.commands.append(command)
    def check_errors(self):
        pass
    def shutdown(self):
        self.shutdown_calls += 1
        for channel in (self.chan1, self.chan2):
            channel.on = False
            channel.auto = False
    def close(self):
        self.closed = True
        self.is_connected = False
class FakeSpot:
    calls = []
    def __init__(self, smu, channel):
        self.smu = smu
        self.channel = channel
    def configure(self, nplc):
        self.calls.append((self.channel, nplc))
        selected = (
            self.smu.chan1 if self.channel == 1 else self.smu.chan2
        )
        selected.nplc = nplc
@pytest.fixture
def connected(pair):
    driver = FakeSMU()
    session = io.DualInstrumentSession(
        build_dual_plan(pair),
        "TESTSERIAL",
        smu_factory=lambda *args, **kwargs: driver,
    )
    session.connect()
    try:
        yield session, driver
    finally:
        session.close()
@pytest.mark.parametrize("channel", [1, 2])
def test_channel_stop_does_not_stop_peer(connected, channel):
    session, driver = connected
    assert session.stop_channel(channel) is True
    selected = driver.chan1 if channel == 1 else driver.chan2
    peer = driver.chan2 if channel == 1 else driver.chan1
    assert selected.on is False
    assert selected.auto is False
    assert peer.on is True
    assert peer.auto is True
    assert driver.shutdown_calls == 0
    assert driver.commands == [f":ABOR:ALL (@{channel})"]
    assert session.stop_evidence[channel]["verified"] is True
@pytest.mark.parametrize("channel", [0, 3, True, "1"])
def test_invalid_channel_rejected_without_writes(connected, channel):
    session, driver = connected
    with pytest.raises(ValueError):
        session.stop_channel(channel)
    assert driver.commands == []
def test_stop_continues_after_one_command_fails(connected):
    session, driver = connected
    driver.chan1.fail_auto = True
    with pytest.raises(io.ChannelStopError):
        session.stop_channel(1)
    assert driver.chan1.on is False
    assert driver.chan2.on is True
    assert 1 not in session.stop_evidence
def test_reported_output_on_cannot_be_accepted(connected):
    session, driver = connected
    driver.chan1.force_on_readback = True
    with pytest.raises(io.ChannelStopError):
        session.stop_channel(1)
    assert 1 not in session.stop_evidence
def test_shared_setup_rejected_while_output_active(
    connected, monkeypatch
):
    session, driver = connected
    monkeypatch.setattr(io, "SpotAcquisition", FakeSpot)
    with pytest.raises(RuntimeError, match="Both outputs"):
        session.configure_shared()
    assert driver.commands == []
def test_shared_setup_configures_both_channels(
    connected, monkeypatch
):
    session, driver = connected
    FakeSpot.calls = []
    monkeypatch.setattr(io, "SpotAcquisition", FakeSpot)
    session.shutdown_all()
    session.configure_shared()
    assert [channel for channel, _ in FakeSpot.calls] == [1, 2]
    assert ":FORM:SREG ASC" in driver.commands
def test_wrong_serial_is_rejected(pair):
    driver = FakeSMU()
    session = io.DualInstrumentSession(
        build_dual_plan(pair),
        "WRONG",
        smu_factory=lambda *args, **kwargs: driver,
    )
    try:
        with pytest.raises(RuntimeError, match="serial"):
            session.connect()
    finally:
        session.close()
    assert driver.closed is True
def test_second_controlling_thread_rejected(connected):
    session, driver = connected
    session.owner = -1
    try:
        with pytest.raises(RuntimeError, match="controlling thread"):
            session.stop_channel(1)
    finally:
        import threading
        session.owner = threading.get_ident()
    assert driver.commands == []
def config_arguments(pair, tmp_path):
    arguments = []
    for index, document in enumerate(pair):
        path = tmp_path / f"channel_{index + 1}.yaml"
        path.write_text(yaml.safe_dump(document), encoding="utf-8")
        arguments += ["--config", str(path)]
    return arguments
def test_probe_default_has_no_connection(
    pair, tmp_path, monkeypatch, capsys
):
    def forbidden(*args, **kwargs):
        pytest.fail("Default probe constructed an instrument.")
    monkeypatch.setattr(io, "_new_smu", forbidden)
    assert io.main(config_arguments(pair, tmp_path)) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["state"] == "dry_run"
    assert report["connected"] is False
    assert report["will_enable_output"] is False
def test_connected_probe_stays_off_and_closes(
    pair, tmp_path, monkeypatch, capsys
):
    driver = FakeSMU()
    monkeypatch.setattr(
        io, "_new_smu", lambda *args, **kwargs: driver
    )
    monkeypatch.setattr(io, "SpotAcquisition", FakeSpot)
    arguments = config_arguments(pair, tmp_path)
    arguments += ["--connect", "--expected-serial", "TESTSERIAL"]
    assert io.main(arguments) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["state"] == "outputs_off_verified"
    assert report["initial_stop_verified"] is True
    assert report["final_stop_verified"] is True
    assert report["session_close_returned"] is True
    assert report["concurrent_execution_available"] is False
    assert driver.chan1.on is False
    assert driver.chan2.on is False
    assert driver.closed is True
def test_connect_requires_explicit_serial(capsys):
    with pytest.raises(SystemExit) as caught:
        io.main([
            "--config", "unused1.yaml",
            "--config", "unused2.yaml",
            "--connect",
        ])
    assert caught.value.code == 2
