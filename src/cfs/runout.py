"""Residual-filament draining and automatic spool refill coordination."""

import math

from .lifecycle import LifecycleStopped
from .native import build_request, slot_mask


class Runout:
    def __init__(self, box):
        self.box = box

    def _calls(self, addresses=(), operation=True):
        guards = [self.box.lifecycle._report_guard(operation=operation)]
        guards.extend(self.box.lifecycle._report_guard(address, operation=operation)
                      for address in set(addresses))

        def call(function, *args, **kwargs):
            for guard in guards:
                guard()
            result = function(*args, **kwargs)
            for guard in guards:
                guard()
            return result
        return call

    def _source_addresses(self):
        source = self.box.last_cmd
        return [] if source is None else [self.box.box_state.get_Tnn_content(source)["addr"]]

    def check_connect(self, addr):
        return self.box.box_state.get_Tn_data(addr, "state") == "connect"

    def communication_get_box_state(self, addr):
        return self._calls([addr])(self.box.send_native, build_request(addr, "GET_BOX_STATE"))

    def set_cool_temp(self):
        box = self.box
        call = self._calls()
        if box.print_stats.state == "paused":
            heater = call(box.toolhead.extruder.get_heater)
            if heater.target_temp >= heater.min_extrude_temp:
                call(box.gcode.run_script_from_command,
                     "SET_GCODE_VARIABLE MACRO=PRINTER_PARAM VARIABLE=hotend_temp VALUE=%s"
                     % heater.target_temp)
            call(box.gcode.run_script_from_command, "M104 S140")
        elif box.print_stats.state != "printing":
            call(box.gcode.run_script_from_command, "M104 S0")

    def _matching_materials(self, source):
        state = self.box.box_state
        content = state.get_Tnn_content(source)
        kind = state.get_Tn_data(content["addr"], "material_type", content["num"])
        color = state.get_Tn_data(content["addr"], "color_value", content["num"])
        candidates = []
        for group in state.Tn_data["same_material"]:
            if group[0] == kind and group[1] == color:
                candidates = group[2]
        return candidates

    def update_Tnn_map(self):
        """Remap every alias of the consumed source to the first available peer."""
        box, state = self.box, self.box.box_state
        source = box.last_cmd
        address = state.get_Tnn_content(source)["addr"]
        guards = [self._calls([address])]

        def call(function, *args, **kwargs):
            def check():
                for guard in guards:
                    guard(lambda: None)
                if box.last_cmd != source:
                    raise LifecycleStopped("CFS remapping source changed")
            check()
            result = function(*args, **kwargs)
            check()
            return result

        for candidate in self._matching_materials(source):
            target_address = state.get_Tnn_content(candidate)["addr"]
            guards.append(self._calls([target_address]))
            if call(box.is_material_available, candidate):
                for logical, physical in list(state.get_Tnn_map().items()):
                    if physical == source:
                        call(box.gcode.run_script_from_command,
                             "BOX_MODIFY_TN %s=%s" % (logical, candidate))
                return

    def extrusion_all_materials(self):
        """Drain in 80 mm segments, including one segment after a clear head.

        The stock 3000 mm ceiling ends normally; success here is its control-flow
        result, not proof that the path is physically empty.
        """
        box = self.box
        call = self._calls(self._source_addresses())
        run = lambda command: call(box.gcode.run_script_from_command, command)
        if call(box.toolhead.get_position)[2] < 25 and call(box.get_filament_sensor_detect):
            run("M400")
            call(box.motion.z_down, distance=25, absolute=True, wait=True)
            if box.move_z:
                call(box.record_tn_move_z, box.move_z)
        temperature = call(box.get_material_target_temp, box.last_cmd)
        speed = call(box.get_material_max_extrusion_speed, box.last_cmd)
        velocity = box.boxcfg.Tn_extrude_velocity[0] if speed is None else speed * 60
        call(box.quickly_wait_heating, temperature)
        for command in ("SET_PIN PIN=fan0 VALUE=0", "G4 P3000", "M400"):
            run(command)
        clear = False
        for consumed in range(80, 3041, 80):
            call(box.motion.go_to_extrude_pos)
            result = call(box.check_and_extrude, 80, velocity, wait=False)
            for address in range(1, 5):
                if call(box.check_connect, address):
                    call(box.communication_get_box_state, address)
                    # Preserve the original absolute reactor deadline.
                    call(box._reactor.pause, 0.2)
            run("M400")
            if result is False:
                return False
            call(box.nozzle_clean)
            if consumed >= 3000 or clear:
                break
            clear = call(box.get_filament_sensor_detect) is False
        call(box.set_cool_temp)
        if box.last_cmd:
            address = box.box_state.get_Tnn_content(box.last_cmd)["addr"]
            call(box.communication_set_box_mode, address, "IDLE")
        return True

    def is_use_ending_material_flush(self, **context):
        box = self.box
        call = self._calls(self._source_addresses())
        call(box.set_head_runout_enabled, False)
        call(box.gcode.run_script_from_command, "M400")
        if call(self.extrusion_all_materials) is False:
            call(box.clear_err)
            call(box.box_state.clear_e_err)
            context = dict(context)
            reason = context.pop("err")
            call(box.lifecycle.record_error, reason, context)
            return False
        if box.last_cmd is not None:
            content = box.box_state.get_Tnn_content(box.last_cmd)
            call(box.box_state.modify_Tn_data, content["addr"], "none", "vender", content["num"])
            call(box.box_state.update_same_material_list, box.store.get_material_name)
            call(box.update_Tnn_map)
        box.is_use_ending_material = False
        box.box_filament_err = False
        call(box.clear_err)
        box.last_cmd_tmp, box.last_cmd = box.last_cmd, None

    def cmd_extrusion_all_materials(self, gcmd):
        box = self.box
        call = self._calls(self._source_addresses())
        if call(self.extrusion_all_materials) is False:
            call(box.clear_err)
            call(box.box_state.clear_e_err)
            call(box.lifecycle.record_error, "macro_extrusion_all_materials_err", {})
            return
        box.last_cmd_tmp, box.last_cmd = box.last_cmd, None
        call(box.set_head_runout_enabled, False)

    def cmd_wait_extrusion_all_materials(self, gcmd):
        box = self.box
        call = self._calls(operation=False)
        while box.inside_auto_refill or box.inside_extrusion_all_materials:
            call(box._reactor.pause, box._reactor.monotonic() + 1)

    def check_material_refill(self):
        box = self.box
        addresses = self._source_addresses()
        if box.error == "filament_err" and box.error_tnn["addr"] is not None:
            addresses.append(box.error_tnn["addr"])
        call = self._calls(addresses)
        if box.error == "filament_err":
            address = box.error_tnn["addr"]
            if address is None:
                return
            call(box.communication_set_box_mode, address, "IDLE")
            call(box.clear_err)
        if box.is_use_ending_material:
            box.is_use_ending_material = False
            call(box.set_head_runout_enabled, False)
            self._schedule_source(self.material_auto_refill)
        elif box.box_state.Tn_data["enable"]:
            call(box.set_head_runout_enabled, False)

    def cmd_check_material_refill(self, gcmd):
        return self.check_material_refill()

    def _schedule_source(self, callback):
        """Capture source/device now, before cooperative deferred work can run."""
        box = self.box
        source = box.last_cmd
        call = self._calls(self._source_addresses(), operation=False)

        def run(eventtime):
            if box.last_cmd != source:
                return
            return call(callback, eventtime)
        call(box.lifecycle.schedule, run)

    def schedule_tighten_up(self):
        self._schedule_source(self.filament_err_tighten_up_event)

    def material_auto_refill(self, eventtime):
        box, state = self.box, self.box.box_state
        source = box.last_cmd
        guards = [self._calls(self._source_addresses(), operation=False)]
        expected_source = source
        handing_off = False

        def call(function, *args, **kwargs):
            def check():
                for guard in guards:
                    guard(lambda: None)
                if not handing_off and box.last_cmd != expected_source:
                    raise LifecycleStopped("CFS refill source changed")
            check()
            result = function(*args, **kwargs)
            check()
            return result

        if not state.Tn_data["auto_refill"] or source is None:
            call(box.gcode.respond_info, "disable material automatic refill")
            state.Tn_data["filament"] = 0
            return
        content = state.get_Tnn_content(source)
        address, slot = content["addr"], content["num"]
        candidates = self._matching_materials(source)
        target = None
        for candidate in candidates:
            candidate_address = state.get_Tnn_content(candidate)["addr"]
            candidate_call = self._calls([address, candidate_address], operation=False)
            guards.append(candidate_call)
            available = call(box.is_material_available, candidate)
            if available:
                target = candidate
                break
        if target is None:
            call(box.gcode.respond_info, "no identical supplies" if not candidates
                 else "no tray with ingredients found")
            state.Tn_data["filament"] = 0
            if not candidates:
                box.last_cmd_tmp, box.last_cmd = source, None
                expected_source = None
            call(box.lifecycle.report_error, "key_filament_err",
                 {"address": address, "request_address": address, "slot": slot})
            call(box.lifecycle.record_error, "filament_err",
                 {"last_tnn": box.last_cmd_tmp, "tnn": box.last_cmd_tmp,
                  "addr": address, "filament_sensor_enable": False})
            return
        call(box.gcode.respond_info, "next_tnn = %s" % target)
        box.last_cmd_tmp, box.last_cmd = source, None
        expected_source = None
        call(state.modify_Tn_data, address, "none", "vender", slot)
        call(state.update_same_material_list, box.store.get_material_name)
        for logical, physical in list(state.get_Tnn_map().items()):
            if physical == source:
                call(box.gcode.run_script_from_command, "BOX_MODIFY_TN %s=%s" % (logical, target))
        logical = next((name for name, physical in state.get_Tnn_map().items()
                        if physical == target), None)
        if logical is None:
            return
        call(box.clear_err)
        box.material_auto_refill_flag = True
        box.inside_auto_refill = True
        # The selected child owns last_cmd from this deliberate handoff onward.
        handing_off = True
        call(box.gcode.run_script_from_command, logical)
        box.inside_auto_refill = False

    def filament_err_tighten_up_event(self, eventtime):
        """Observe connection clearance and commanded E before tightening."""
        box = self.box
        source = box.last_cmd
        if source is None:
            return
        content = box.box_state.get_Tnn_content(source)
        address, slot = content["addr"], content["num"]
        guarded = self._calls([address], operation=False)

        def current():
            guarded(lambda: None)
            if box.last_cmd != source:
                raise LifecycleStopped("CFS runout source changed")

        def call(function, *args, **kwargs):
            current()
            result = guarded(function, *args, **kwargs)
            current()
            return result

        def pause(delay=5):
            call(box._reactor.pause, box._reactor.monotonic() + delay)
            current()

        def sample(data):
            current()
            mask = box.retraction._sample(call, address, "GET_FILAMENT_SENSOR_STATE", data)
            if mask is None:
                raise RuntimeError("fresh ordinary runout-sensor observation required")
            return mask

        while sample(b"\x01") & slot_mask(slot):
            pause()
        def position_e():
            value = call(box.toolhead.get_position)[3]
            if not math.isfinite(value):
                raise ValueError("finite commanded E position required")
            return value

        baseline = position_e()
        while position_e() - baseline < 120:
            pause(3.0)
        current()
        call(box.communication_set_box_mode, address, "IDLE")
        call(box.communication_tighten_up_enable, address, "ENABLE")
        while box.print_stats.state in ("printing", "paused"):
            current()
            if box.heart_process_enable[address - 1]:
                material = sample(b"\x00")
                vendor = box.box_state.get_Tn_data(address, "vender", slot)
                if material & slot_mask(slot) and vendor != "none":
                    call(box.communication_tighten_up_enable, address, "DISABLE")
                    box.box_state.Tn_data["filament"] = 1
                    return
            pause()
