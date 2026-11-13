"""Coordinate tool selection through the existing mechanical phases."""


class ToolChange:
    def __init__(self, box):
        self.box = box

    def Tn_action(self, vtnn):
        """Select a mapped physical tool, retaining phase-specific failure tails.

        Only operation ownership is released unconditionally. Fan, Z and motion
        restoration belong to the successful paths, including their distinct
        continuation behavior.
        """
        box, state = self.box, self.box.box_state
        if state.get_Tn_data(0, uppart="enable") == 0:
            return True
        if state.get_Tn_data(0, uppart="state") != "connect":
            return False
        guards = [box.host_guard()]

        def guard():
            for check in guards:
                check()

        def call(function, *args, **kwargs):
            guard()
            result = function(*args, **kwargs)
            guard()
            return result

        call(box.record_tn_cur_tnn, vtnn)
        tower = call(box.get_purge_in_prime_tower)
        source = box.last_cmd
        target = state.get_Tnn_map()[vtnn]
        content = state.get_Tnn_content(target)
        address, slot = content["addr"], content["num"]
        if state.get_Tn_data(address, "state") != "connect":
            return False
        old_content = state.get_Tnn_content(source) if source else None
        box.operations.begin(source, target, address)
        serial = box.operations._serial

        def run(command):
            return call(box.gcode.run_script_from_command, command)

        def record_z():
            if box.move_z:
                call(box.record_tn_move_z, box.move_z)

        def fail(reason, context):
            call(box.lifecycle.record_error, reason, context)
            return False

        try:
            guards = [box.host_guard(address)]
            if old_content:
                guards.append(box.host_guard(old_content["addr"]))
            if old_content == content and box.error in (None, "filament_err"):
                if box.box_state.is_continue_print and call(box.get_filament_sensor_detect) is True:
                    run("M400")
                    call(box.motion.z_down, distance=25, absolute=True, wait=True)
                    record_z()
                    call(box.motion.go_to_extrude_pos)
                    call(box.communication_set_box_mode, address, "PRINT", slot)
                    velocity, _ = call(box.get_flush_velocity, source, source)
                    _, _, temperature = call(box.get_flush_temp, source, source)
                    result = call(box.Tn_Extrude, box.boxcfg.Tn_extrude, velocity, temperature)
                    if result is False:
                        call(state.clear_e_err)
                        return fail("e_err_tn_extrude_err", {"vtnn": vtnn})
                    call(box.nozzle_clean)
                    call(box.motion.move_to_safe_pos)
                    box.box_state.is_continue_print = False
                    call(box.motion.z_restore)
                    return True
                if call(box.get_filament_sensor_detect) is True:
                    call(box.communication_set_box_mode, address, "PRINT", slot)
                    temperature = call(box.get_material_target_temp, target)
                    call(box.quickly_wait_heating, temperature)
                    return True

            if box.is_use_ending_material:
                from .box import MissingProtocolEffect

                # Reject this unfinished route before saving fans or moving E/Z.
                raise MissingProtocolEffect("ending-material drain requires the stage-8 executor")
            if box.box_state.is_continue_print:
                box.box_state.is_continue_print = False
            call(box.motion.save_printer_accel, box.toolhead.max_velocity, box.toolhead.max_accel)
            call(box.motion.close_model_fan)
            heater = call(box.toolhead.extruder.get_heater)
            if heater.can_extrude:
                run("G91\n G1 E-4 F4800\n G90")
            call(box.motion.z_down, distance=0.8)
            record_z()
            if box.is_use_ending_material:
                call(box.is_use_ending_material_flush,
                     err="z_err_extrusion_all_materials_err", vtnn=vtnn)
            if box.last_cmd is not None or call(box.get_filament_sensor_detect):
                if call(box.move_to_cut) is False:
                    return fail("cut_err", {"vtnn": vtnn})
            if not tower:
                call(box.motion.z_down, distance=25, absolute=True, wait=True)
                record_z()
            context = {"last_tnn": source, "tnn": target}
            if box.is_use_ending_material:
                call(box.is_use_ending_material_flush,
                     err="z_err_extrusion_all_materials_err", vtnn=vtnn)
            else:
                result = call(box.box_retrude_material)
                run("M400")
                if result is False:
                    return fail("retrude_err", context)
                if tower:
                    # This route belongs to the slicer's prime-tower sequence.
                    safe_y, = box.motion._positions("safe_pos_y")
                    run("G90\n G0 Y%.2f F24000\n M400" % (safe_y + 1))
                else:
                    call(box.motion.go_to_extrude_pos)
                    call(box.cutter.strike_clean_box, wait=False)
            result = call(box.box_extrude_material, target)
            run("M400")
            if result is False:
                return fail("box_extrude_err", context)
            if call(box.extruder_extrude, target) is False:
                return fail("extruder_extrude_err", context)
            run("M400")
            if not tower:
                for attempt in range(3):
                    result = call(box.material_change_flush, source, target,
                                  error_report=attempt == 2)
                    if result or attempt == 2:
                        break
                    if box.last_err and "key845" not in box.last_err:
                        break
                    run("BOX_MOVE_TO_SAFE_POS\n M400")
                    call(box._reactor.pause, box._reactor.monotonic() + 1)
                    call(box.motion.go_to_extrude_pos)
                if result is False:
                    return fail("flush_err", dict(context, cover=True))
            call(box.motion.restore_fan)
            heater = call(box.toolhead.extruder.get_heater)
            if heater.can_extrude and box.boxcfg.tn_final_extrude_len != 0:
                run("G91\n G1 E%.2f F4800\n G90" % box.boxcfg.tn_final_extrude_len)
            call(box.motion.z_restore)
            call(box.record_tn_move_z, 0)
            run("G92 E0")
            run("G90")
            call(box.motion.restore_printer_accel)
            run("M400")
            if box.material_auto_refill_flag:
                box.material_auto_refill_flag = False
                run("RESUME")
            return True
        finally:
            if box.operations.active and box.operations._serial == serial:
                box.operations.cancel()
