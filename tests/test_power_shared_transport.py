"""Fake-wire tests only; no VISA, connections to devices, or qualification."""
from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
import json
import pytest
from drivers.smu_channel import SMUChannel
from experiments.constant_power import PowerLimits
from experiments.dual_instrument_io import DualInstrumentSession
from experiments.dual_preconditioning import build_dual_plan
from experiments.power_planning import build_dual_constant_power_sweep_plan
from experiments.power_shared_transport import SharedPowerSession, SharedPowerTransportError
from test_charge_current_sweep import profile
from test_constant_power_planning import power_profile
from test_power_adapter import VirtualClock


class Wire:
    """Stateful SCPI fake using real SMUChannel/SpotAcquisition implementations."""
    def __init__(self, resource):
        self.resource_address = resource
        self.identity = "Keysight Technologies,B2902B,TESTSERIAL,1.0"
        self.is_connected = False
        self.clock = VirtualClock()
        self.commands, self.failures, self.measurements = [], {}, []
        self.shutdown_count = self.close_count = self.connect_count = 0
        self.status, self.measure_voltage, self.measure_current = {}, {}, {}
        self.responses = {":FORM:DATA?":"ASC", ":FORM:ELEM:SENS?":"VOLT,CURR,TIME,STAT",
            ":SYST:GRO:DEF?":"(@1,2)", "*OPC?":"1", ":SYST:TIME:TIM:COUN:RES:AUTO?":"1"}
        self.chan1, self.chan2 = SMUChannel(self,1), SMUChannel(self,2)
        for c in (1,2):
            self.responses.update({f":OUTP{c}?":"0", f":OUTP{c}:ON:AUTO?":"0", f":OUTP{c}:OFF:MODE?":"HIZ",
                f":SOUR{c}:FUNC:MODE?":"CURR", f":SOUR{c}:FUNC:SHAP?":"DC", f":SOUR{c}:CURR:MODE?":"FIX",
                f":SOUR{c}:VOLT:MODE?":"FIX", f":SOUR{c}:CURR?":"0", f":SOUR{c}:VOLT?":"0",
                f":SENS{c}:VOLT:PROT?":"3.81", f":SENS{c}:CURR:PROT?":"0.4", f":SENS{c}:REM?":"1",
                f":SENS{c}:VOLT:NPLC?":"1", f":SENS{c}:VOLT:NPLC? MIN":"0.001", f":SENS{c}:VOLT:NPLC? MAX":"100",
                f':SENS{c}:FUNC:STAT? "VOLT"':"1", f':SENS{c}:FUNC:STAT? "CURR"':"1"})

    def _touch(self, command):
        self.commands.append(command)
        self.clock.time += 0.00001
        if command in self.failures:
            raise self.failures[command]

    def connect(self):
        self.connect_count += 1
        self.is_connected = True

    def _query(self, command):
        self._touch(command)
        if command.startswith(":MEAS? "):
            c = int(command.split("(@")[1][0])
            self.measurements.append(c)
            return f"{self.measure_voltage.get(c,3.0)},{self.measure_current.get(c,self.responses[f':SOUR{c}:CURR?'])},0,{self.status.get(c,1)}"
        return self.responses[command]

    def _write(self, command):
        self._touch(command)
        if command.startswith(":ABOR:"):
            return
        base, value = command.split(" ",1)
        self.responses[base+"?"] = {"ON":"1", "OFF":"0"}.get(value,value)

    def check_errors(self):
        self._touch("CHECK_ERRORS")

    def shutdown(self):
        self.shutdown_count += 1
        self._touch("GLOBAL_SHUTDOWN")
        for c in (1,2):
            self.responses[f":OUTP{c}?"] = "0"
            self.responses[f":OUTP{c}:ON:AUTO?"] = "0"

    def close(self):
        self.close_count += 1
        self._touch("CLOSE")
        self.is_connected = False


@pytest.fixture
def context(profile):
    first = power_profile(profile)
    first["execution"] = {"expected_serial":"TESTSERIAL", "maximum_voltage_v":3.81, "maximum_current_a":0.4}
    second = deepcopy(first)
    second["instrument"]["channel"] = 2
    second["instrument"]["nplc"] = 2
    second["dut"]["specimen_id"] = "second_separate_cell"
    documents = [first, second]
    plan = build_dual_constant_power_sweep_plan(documents)
    driver = Wire(plan["resource"])
    session = DualInstrumentSession(build_dual_plan([p["initial_preparation"]["configuration"] for p in plan["channels"]]),
        "TESTSERIAL", smu_factory=lambda *args, **kwargs:driver)
    session.connect()  # Fake only; production owner has no connect/factory API.
    def owner(**changes):
        args = dict(session=session, clock=driver.clock.now, output_authorized=True)
        args.update(changes)
        return SharedPowerSession(documents, **args)
    def limits(channel=1, condition=0):
        return PowerLimits(**plan["channels"][channel-1]["conditions"][condition]["controller_limits"])
    return documents, driver, session, owner, limits


def borrow(owner, context, channel=1, condition=0):
    return owner.borrow(channel, context[4](channel,condition), maximum_probe_current_a=0.001)


def activate(owner, context, channel=1):
    endpoint = borrow(owner,context,channel)
    endpoint.prepare();endpoint.set_output(True)
    return endpoint


def test_constructor_does_no_io_and_context_performs_off_only_shared_setup(context):
    docs, wire, session, make, _ = context
    original = deepcopy(docs)
    before = list(wire.commands)
    owner = make()
    assert wire.commands == before and wire.connect_count == 1
    with owner:
        assert wire.responses[":SYST:GRO:DEF?"] == "(@1),(@2)"
        assert wire.responses[":SYST:TIME:TIM:COUN:RES:AUTO?"] == "0"
        assert not any(c.endswith(" ON") and c.startswith(":OUTP") for c in wire.commands)
        assert owner.startup_evidence["physical_qualification"] is False
    assert docs == original and wire.close_count == 1 and not session.connected
    assert owner.cleanup_report["outputs_reported_off"] is True
    assert json.loads(json.dumps(owner.cleanup_report)) == owner.cleanup_report


@pytest.mark.parametrize("channel", [1,2])
def test_second_channel_prepares_activates_reads_and_stops_while_peer_stays_active(context, channel):
    wire, make = context[1], context[3]
    with make() as owner:
        peer = activate(owner,context,3-channel)
        peer.set_current(-0.1)
        before = owner._fingerprint(3-channel)
        mark = len(wire.commands)
        endpoint = activate(owner,context,channel)
        assert endpoint.read_fresh().channel == channel
        endpoint.set_current(-0.2)
        assert endpoint.get_current_setpoint() == -0.2
        endpoint.stop()
        assert owner._fingerprint(3-channel) == before
        assert peer.get_output_state() is True and wire.close_count == 0 and wire.shutdown_count == 0
        writes = [c for c in wire.commands[mark:] if "?" not in c and c != "CHECK_ERRORS"]
        assert not any(c.startswith((":FORM:",":SYST:")) or "(@1,2)" in c for c in writes)
        assert f":ABOR:ALL (@{channel})" in writes and f":ABOR:ALL (@{3-channel})" not in writes
        assert wire.measurements[-1] == channel
    assert wire.shutdown_count == 1 and wire.close_count == 1


def test_selected_preparation_does_not_call_off_only_spot_configure_again(context, monkeypatch):
    from acquisition.spot_acquisition import SpotAcquisition
    with context[3]() as owner:
        activate(owner,context,1)
        def forbidden(*args, **kwargs):
            pytest.fail("Per-phase setup attempted global formatting")
        monkeypatch.setattr(SpotAcquisition,"configure",forbidden)
        activate(owner,context,2)


def test_borrowed_stop_does_not_expose_global_shutdown_or_close(context):
    with context[3]() as owner:
        endpoint = borrow(owner,context)
        assert not hasattr(endpoint,"shutdown_all") and not hasattr(endpoint,"close_session")
        endpoint.prepare();endpoint.set_output(True);endpoint.stop()
        next_phase = borrow(owner,context)
        next_phase.prepare();next_phase.set_output(True)
        assert next_phase is not endpoint


def test_default_authorization_rejected_before_any_io_or_claim(context):
    docs, wire, session, _, _ = context
    owner = SharedPowerSession(docs, session=session, clock=wire.clock.now)
    before = list(wire.commands)
    with pytest.raises(SharedPowerTransportError,match="authorization"):
        owner.__enter__()
    assert wire.commands == before and not owner.claimed and session.connected


def test_pre_enter_borrow_rejection_does_not_close_caller_session(context):
    owner = context[3]()
    before = list(context[1].commands)
    with pytest.raises(SharedPowerTransportError,match="not available"):
        borrow(owner,context)
    assert context[1].commands == before and context[2].connected


@pytest.mark.parametrize("kind", ["serial", "resource", "disconnected", "plan"])
def test_constructor_mismatch_does_not_do_io(context, kind):
    wire, session = context[1:3]
    if kind == "serial":wire.identity = "Keysight,B2902B,OTHER,FW"
    elif kind == "resource":wire.resource_address = "OTHER::INSTR"
    elif kind == "disconnected":wire.is_connected = False
    else:session.plan["resource"] = "OTHER::INSTR"
    before = list(wire.commands)
    with pytest.raises(SharedPowerTransportError):context[3]()
    assert wire.commands == before and wire.close_count == 0


def test_existing_output_on_rejects_entry_and_transferred_owner_cleans_up(context):
    wire = context[1];wire.responses[":OUTP1?"] = "1"
    owner = context[3]()
    with pytest.raises(SharedPowerTransportError,match="Both outputs"):
        owner.__enter__()
    assert ":SYST:GRO:DEF (@1),(@2)" not in wire.commands
    assert wire.shutdown_count == 1 and wire.close_count == 1


def test_wrong_thread_operation_does_not_touch_driver_and_owner_can_still_cleanup(context):
    wire = context[1]
    with context[3]() as owner:
        endpoint = activate(owner,context)
        before = list(wire.commands)
        with ThreadPoolExecutor(max_workers=1) as pool:
            with pytest.raises(SharedPowerTransportError,match="controlling thread"):
                pool.submit(endpoint.read_fresh).result()
        assert wire.commands == before and wire.close_count == 0
    assert wire.close_count == 1


def test_same_driver_cannot_have_two_owners_and_no_retry_after_close(context):
    with context[3]() as owner:
        before = list(context[1].commands)
        with pytest.raises(SharedPowerTransportError,match="already claimed"):
            context[3]()
        assert context[1].commands == before
    with pytest.raises(SharedPowerTransportError):owner.__enter__()
    assert context[1].close_count == 1


@pytest.mark.parametrize("command,value", [(":SYST:GRO:DEF?","(@1,2)"), (":FORM:DATA?","REAL"),
    (":FORM:ELEM:SENS?","VOLT,CURR"), (":SYST:TIME:TIM:COUN:RES:AUTO?","1")])
def test_shared_readback_changes_stop_both_before_current_command(context, command, value):
    wire = context[1]
    with pytest.raises(SharedPowerTransportError):
        with context[3]() as owner:
            endpoint = activate(owner,context)
            wire.responses[command] = value
            mark = len(wire.commands)
            endpoint.set_current(-0.2)
    assert ":SOUR1:CURR -0.2" not in wire.commands[mark:]
    assert wire.shutdown_count == 1 and wire.close_count == 1


@pytest.mark.parametrize("status", [9,8193,3,0])
def test_fault_frames_and_acquisition_brackets_survive_immediate_fail_both_cleanup(context,status):
    wire = context[1]
    with pytest.raises(SharedPowerTransportError) as caught:
        with context[3]() as owner:
            endpoint = activate(owner,context)
            wire.status[1] = status
            endpoint.read_fresh()
    assert caught.value.power_frame.status == status
    a,b = caught.value.power_read_bracket_s
    assert a < b and wire.shutdown_count == 1 and wire.close_count == 1
    assert caught.value.shared_power_cleanup_report["outputs_reported_off"]


@pytest.mark.parametrize("field,value", [("voltage",100), ("current",0.01)])
def test_invalid_probe_voltage_or_leakage_retains_frame_and_closes(context,field,value):
    wire = context[1]
    with pytest.raises(SharedPowerTransportError) as caught:
        with context[3]() as owner:
            endpoint = activate(owner,context)
            (wire.measure_voltage if field == "voltage" else wire.measure_current)[1] = value
            endpoint.read_fresh()
    assert caught.value.power_frame.channel == 1 and wire.close_count == 1


@pytest.mark.parametrize("current", [0,0.1,-0.5,True,float("inf")])
def test_invalid_current_commands_never_reach_setter(context,current):
    wire = context[1]
    with pytest.raises((SharedPowerTransportError,ValueError)):
        with context[3]() as owner:
            endpoint = activate(owner,context)
            before = wire.commands.count(":SOUR1:CURR -0.5")
            endpoint.set_current(current)
    assert wire.commands.count(":SOUR1:CURR -0.5") == before and wire.close_count == 1


def test_suppressed_endpoint_error_cannot_turn_context_into_success(context):
    original = OSError("original current write")
    wire = context[1]
    with pytest.raises(OSError) as caught:
        with context[3]() as owner:
            endpoint = activate(owner,context)
            wire.failures[":SOUR1:CURR -0.2"] = original
            try:endpoint.set_current(-0.2)
            except OSError:pass
            assert owner.closed and not wire.is_connected
    assert caught.value is original and wire.close_count == 1


def test_close_is_idempotent_and_does_not_repeat_io(context):
    owner = context[3]()
    with owner:
        activate(owner,context)
    before = list(context[1].commands)
    assert owner.close() == owner.cleanup_report
    assert context[1].commands == before



def test_two_actual_cp_controllers_can_be_stepped_over_borrowed_channels(context):
    from experiments.constant_power import ConstantPowerController
    wire = context[1]
    with context[3]() as owner:
        controllers, endpoints = {}, {}
        for c in (1,2):
            endpoint = borrow(owner,context,c)
            setup = endpoint.prepare()
            controller = ConstantPowerController(endpoint.limits)
            controller.arm_zero_current_probe(now_s=wire.clock.now(), outputs_verified_off=setup["selected_output_off"],
                current_source_verified=setup["current_source"], zero_current_setpoint_a=setup["current_setpoint_a"],
                maximum_probe_current_a=endpoint.probe_ceiling)
            endpoint.set_output(True)
            controller.confirm_zero_current_probe_enabled(now_s=wire.clock.now(), output_reported_on=True)
            endpoints[c], controllers[c] = endpoint, controller
        for iteration in range(4):
            for c in list(endpoints):
                endpoint, controller = endpoints[c], controllers[c]
                if iteration == 3:wire.measure_voltage[c] = endpoint.limits.cutoff_voltage_v
                before = wire.clock.now()
                frame = endpoint.read_fresh()
                sample = frame.power_sample(before)
                decision = (controller.start_from_zero_current_probe(now_s=wire.clock.now(),sample=sample)
                            if controller.state == "probing" else controller.update(now_s=wire.clock.now(),sample=sample))
                if decision.action == "set_current":
                    endpoint.set_current(decision.current_setpoint_a)
                    value = endpoint.get_current_setpoint()
                    controller.acknowledge_setpoint(now_s=wire.clock.now(), readback_current_a=value)
                elif decision.action == "stop":
                    endpoint.stop();del endpoints[c]
            wire.clock.sleep(0.21)
        assert endpoints == {} and all(c.state == "completed" for c in controllers.values())
        assert wire.close_count == 0 and wire.shutdown_count == 0
    assert wire.close_count == 1 and wire.measurements == [1,2]*4


def test_owner_cleanup_preserves_body_interrupt_and_attempts_all_stops(context):
    wire = context[1]
    original = KeyboardInterrupt("primary body interrupt")
    with pytest.raises(KeyboardInterrupt) as caught:
        with context[3]() as owner:
            activate(owner,context,1);activate(owner,context,2)
            wire.failures["GLOBAL_SHUTDOWN"] = OSError("secondary shutdown")
            wire.failures[":ABOR:ALL (@1)"] = KeyboardInterrupt("secondary scoped abort")
            raise original
    assert caught.value is original
    assert ":OUTP1 OFF" in wire.commands and ":OUTP2 OFF" in wire.commands
    assert wire.close_count == 1 and owner.cleanup_report["outputs_reported_off"]
    assert len(original.__notes__) >= 2


def test_cleanup_keeps_known_on_output_visible_after_failed_stops(context):
    wire = context[1]
    original = OSError("primary body error")
    with pytest.raises(OSError) as caught:
        with context[3]() as owner:
            activate(owner,context)
            wire.failures["GLOBAL_SHUTDOWN"] = OSError("shutdown failed")
            wire.failures[":OUTP1 OFF"] = OSError("off command failed")
            raise original
    assert caught.value is original and wire.close_count == 1
    report = original.shared_power_cleanup_report
    assert report["channels"][0]["output_state"] is True and report["outputs_reported_off"] is False
    assert report["session_close_returned"] is True


def test_close_failure_cannot_be_reported_as_success(context):
    wire = context[1]
    with pytest.raises(OSError,match="close failed") as caught:
        with context[3]() as owner:
            wire.failures["CLOSE"] = OSError("close failed")
    assert wire.close_count == 1
    assert caught.value.shared_power_cleanup_report["session_close_returned"] is False
    with pytest.raises(OSError):owner.close()
    assert wire.close_count == 1


def test_peer_mode_and_setpoint_are_not_mutated_by_selected_preparation(context):
    wire = context[1]
    with context[3]() as owner:
        # Stand-in for a future native CV peer. No acquisition/recording is claimed.
        wire.responses.update({":SOUR2:FUNC:MODE?":"VOLT", ":SOUR2:VOLT?":"3.8", ":OUTP2?":"1"})
        before = owner._fingerprint(2)
        endpoint = activate(owner,context,1)
        endpoint.set_current(-0.2);endpoint.read_fresh();endpoint.stop()
        assert owner._fingerprint(2) == before


def test_mutating_peer_configuration_is_terminal_even_if_output_stays_on(context):
    wire = context[1]
    with pytest.raises(SharedPowerTransportError,match="peer configuration"):
        with context[3]() as owner:
            peer = activate(owner,context,2);peer.set_current(-0.1)
            endpoint = activate(owner,context,1)
            original = wire._write
            def bad_write(command):
                original(command)
                if command == ":SOUR1:CURR -0.2":wire.responses[":SOUR2:CURR?"] = "-0.2"
            wire._write = bad_write
            endpoint.set_current(-0.2)
    assert wire.close_count == 1 and wire.shutdown_count == 1


def test_replacing_session_driver_does_not_redirect_cleanup_to_foreign_driver(context):
    wire, session = context[1:3]
    foreign = Wire(wire.resource_address);foreign.is_connected = True
    with pytest.raises(SharedPowerTransportError,match="ownership changed"):
        with context[3]() as owner:
            endpoint = activate(owner,context)
            session.smu = foreign
            endpoint.read_fresh()
    assert wire.close_count == 1 and foreign.close_count == 0 and foreign.commands == []


def test_clock_failure_after_acquisition_keeps_frame_and_does_not_block_cleanup(context):
    wire = context[1]
    with pytest.raises(ValueError) as caught:
        with context[3]() as owner:
            endpoint = activate(owner,context)
            original = wire._query
            def bad_clock(command):
                value = original(command)
                if command.startswith(":MEAS?"):wire.clock.time = float("nan")
                return value
            wire._query = bad_clock
            endpoint.read_fresh()
    assert caught.value.power_frame.channel == 1 and caught.value.power_read_bracket_s is None
    assert wire.close_count == 1 and caught.value.shared_power_cleanup_report["outputs_reported_off"]


def test_noncanonical_limits_reject_before_preparation_commands(context):
    from dataclasses import replace
    wire = context[1]
    with pytest.raises(SharedPowerTransportError,match="canonical"):
        with context[3]() as owner:
            mark = len(wire.commands)
            owner.borrow(1,replace(context[4](),power_w=0.6),maximum_probe_current_a=0.001)
    assert not any(c.startswith(":SOUR1:CURR ") for c in wire.commands[mark:])


def test_old_lease_cannot_be_used_after_a_new_phase_is_borrowed(context):
    with pytest.raises(SharedPowerTransportError,match="terminal/stale"):
        with context[3]() as owner:
            endpoint = activate(owner,context);endpoint.stop()
            borrow(owner,context)
            endpoint.read_fresh()
    assert context[1].close_count == 1


def test_constructor_rejects_placeholder_serial_without_driver_io(context):
    docs,wire,session,_,_ = context
    for doc in docs:doc["execution"]["expected_serial"] = "REPLACE_SERIAL"
    before = list(wire.commands)
    with pytest.raises((SharedPowerTransportError,ValueError),match="serial"):
        SharedPowerSession(docs,session=session,clock=wire.clock.now,output_authorized=True)
    assert wire.commands == before and wire.close_count == 0


def test_identity_change_before_entry_is_rejected_without_claiming(context):
    owner = context[3]()
    wire = context[1];wire.identity = "Keysight,B2902B,OTHER,FW"
    before = list(wire.commands)
    with pytest.raises(SharedPowerTransportError,match="before ownership"):
        owner.__enter__()
    assert wire.commands == before and not owner.claimed and wire.close_count == 0


def test_transport_is_not_accepted_by_simulation_adapter(context,tmp_path):
    from experiments.power_adapter import run_simulated_power_phase
    with context[3]() as owner:
        endpoint = borrow(owner,context)
        before = list(context[1].commands)
        with pytest.raises(ValueError,match="simulation-only"):
            run_simulated_power_phase(endpoint.limits,tmp_path/"never",backend=endpoint,clock=context[1].clock.now,
                sleep=context[1].clock.sleep,channel=1,maximum_probe_current_a=0.001)
        assert context[1].commands == before and not (tmp_path/"never").exists()


def test_no_factory_or_connection_is_reached(context,monkeypatch):
    import experiments.dual_instrument_io as io
    import experiments.run_live as single
    def forbidden(*args,**kwargs):pytest.fail("Unexpected connection factory")
    monkeypatch.setattr(io,"_new_smu",forbidden);monkeypatch.setattr(single,"_new_smu",forbidden)
    with context[3]() as owner:activate(owner,context)
    assert context[1].connect_count == 1


def test_public_dual_cp_execution_dispatches_to_the_dual_launcher(context,tmp_path,capsys):
    import smu
    import yaml
    paths = []
    for c,doc in enumerate(context[0]):
        p = tmp_path/f"cp_{c}.yaml";p.write_text(yaml.safe_dump(doc));paths.append(str(p))
    from unittest import mock
    with mock.patch("experiments.power_dual_execution.run_configured_dual_power_sweep",
                    return_value=0) as launcher:
        assert smu.main(["--config",paths[0],"--config",paths[1],"--execute"]) == 0
    assert launcher.call_count == 1



def test_invalid_initial_clock_rejected_before_ownership_or_driver_io(context):
    owner = context[3](clock=lambda:float("nan"))
    before = list(context[1].commands)
    with pytest.raises(ValueError):owner.__enter__()
    assert context[1].commands == before and not owner.claimed and context[2].connected


def test_unexpected_output_after_shared_setup_is_not_accepted(context):
    session,wire = context[2],context[1]
    original = session.configure_shared
    def bad_setup():
        original();wire.responses[":OUTP1?"] = "1"
    session.configure_shared = bad_setup
    with pytest.raises(SharedPowerTransportError,match="leave both channels"):
        context[3]().__enter__()
    assert wire.close_count == 1 and wire.shutdown_count == 1



def test_current_readback_return_is_range_checked_even_if_it_changes_between_queries(context):
    with pytest.raises(SharedPowerTransportError,match="negative range"):
        with context[3]() as owner:
            endpoint = activate(owner,context);endpoint.set_current(-0.2)
            count = 0
            def changed():
                nonlocal count
                count += 1
                return -0.2 if count == 1 else 0.1
            endpoint.selected.get_current_setpoint = changed
            endpoint.get_current_setpoint()
    assert context[1].close_count == 1


def test_unexpected_selected_output_off_fails_both(context):
    with pytest.raises(SharedPowerTransportError,match="output transition"):
        with context[3]() as owner:
            endpoint = activate(owner,context)
            context[1].responses[":OUTP1?"] = "0"
            endpoint.get_output_state()
    assert context[1].shutdown_count == 1 and context[1].close_count == 1
