"""Shared private power-phase control loop; caller selects evidence and owns cleanup."""
from dataclasses import asdict
from experiments.power_adapter import PowerFrame, PowerAdapterError, _validate_setup, _wait


def drive_power_phase(limits, *, backend, controller, host, recorder, channel,
                     probe_ceiling, maximum_samples, sleep):
    recorder.event("setup_requested", host_time_s=host.now())
    setup = backend.prepare(limits)
    recorder.event("setup_returned", host_time_s=host.now(), readbacks=setup)
    _validate_setup(setup, limits, channel)
    controller.arm_zero_current_probe(
        now_s=host.now(), outputs_verified_off=True, current_source_verified=True,
        zero_current_setpoint_a=setup["current_setpoint_a"], maximum_probe_current_a=probe_ceiling,
    )
    recorder.event("controller_armed", host_time_s=controller.started_s)
    recorder.event("probe_output_on_requested", host_time_s=host.now())
    controller.watchdog(now_s=host.now())
    backend.set_output(True)
    recorder.event("probe_output_on_command_returned", host_time_s=host.now())
    controller.watchdog(now_s=host.now())
    output_on = backend.get_output_state()
    controller.confirm_zero_current_probe_enabled(now_s=host.now(), output_reported_on=output_on)
    recorder.event("probe_output_on_verified", host_time_s=host.now(),
                   controller_probe_enabled_s=controller.probe_enabled_s, output_reported_on=output_on)
    stage = "zero_current_probe"
    while True:
        if recorder.sample_count >= maximum_samples:
            raise PowerAdapterError("Sample budget exhausted before successful cutoff.")
        controller.watchdog(now_s=host.now())
        started = host.now()
        frame = backend.read_fresh()
        finished = host.now()
        if not isinstance(frame, PowerFrame):
            raise PowerAdapterError("Backend did not return a paired PowerFrame.")
        recorder.sample(asdict(frame), stage=stage, started_s=started,
                        finished_s=finished, requested_power_w=limits.power_w)
        if frame.channel != channel:
            raise PowerAdapterError("Measurement channel mismatch.")
        sample = frame.power_sample(started)
        if stage == "zero_current_probe":
            decision = controller.start_from_zero_current_probe(now_s=host.now(), sample=sample)
        else:
            decision = controller.update(now_s=host.now(), sample=sample)
        recorder.event("control_decision", record_index=recorder.sample_count - 1, **decision.to_dict())
        if decision.action == "stop":
            return {"stop_reason": decision.stop_reason,
                    "decision_stop_time_s": decision.host_time_s}
        if decision.action == "set_current":
            recorder.event("current_command_requested", host_time_s=host.now(),
                           current_setpoint_a=decision.current_setpoint_a)
            controller.watchdog(now_s=host.now())
            backend.set_current(decision.current_setpoint_a)
            recorder.event("current_command_returned", host_time_s=host.now(),
                           current_setpoint_a=decision.current_setpoint_a)
            controller.watchdog(now_s=host.now())
            readback = backend.get_current_setpoint()
            recorder.event("current_readback", host_time_s=host.now(), current_setpoint_a=readback)
            controller.acknowledge_setpoint(now_s=host.now(), readback_current_a=readback)
            recorder.event("current_setpoint_acknowledged", host_time_s=host.now(),
                           controller_acknowledged_at_s=controller.last_ack_s, current_setpoint_a=readback)
        stage = "power_discharge"
        _wait(controller, host, sleep, started + limits.update_interval_s)
