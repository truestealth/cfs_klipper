"""Replay saved recovery phases without treating normal returns as completion."""

import logging

from .errors import _ERRORS


_MACRO_ERRORS = frozenset("macro_" + name for name in (
    "cut_err", "retrude_err", "box_extrude_err", "extruder_extrude_err", "flush_err"))


class Recovery:
    def __init__(self, box):
        self.box = box

    def _calls(self):
        """Allow child phase ownership while retaining the saved device identities."""
        box = self.box
        checks = [box.lifecycle._report_guard(operation=False)]
        context = box.error_tnn
        tools = [box.last_cmd, context.get("last_tnn"), context.get("tnn")]
        virtual = context.get("vtnn")
        if virtual in box.box_state.Tnn_map:
            tools.append(box.box_state.Tnn_map[virtual])
        addresses = {box.box_state.Tnn_content[tool]["addr"] for tool in tools
                     if tool in box.box_state.Tnn_content}
        if context.get("addr") in range(1, 5):
            addresses.add(context["addr"])
        checks.extend(box.lifecycle._report_guard(address, operation=False)
                      for address in addresses)

        def call(function, *args, **kwargs):
            for check in checks:
                check()
            result = function(*args, **kwargs)
            for check in checks:
                check()
            return result

        for check in checks:
            check()
        return call

    def clear_err(self):
        """Clear host bookkeeping without changing filament or refill state."""
        box = self.box
        box.error = box.last_err = None
        box.error_next_macro = []
        for key in ("last_tnn", "tnn", "addr", "vtnn"):
            box.error_tnn[key] = None

    def error_clear(self):
        box = self.box
        call = self._calls()
        reason, context = box.error, box.error_tnn
        if reason not in _ERRORS:
            logging.warning("Unknown CFS error: %s", reason)
            call(self.clear_err)
            return
        if box.material_auto_refill_flag:
            box.material_auto_refill_flag = False
        address, stop_on_false = None, False
        if reason in ("retrude_err", "macro_retrude_err"):
            source = context["last_tnn"]
            address = (box.box_state.get_Tnn_content(source)["addr"]
                       if source is not None else context["addr"] or None)
            stop_on_false = True
        elif reason == "print_end_err":
            address = box.box_state.get_Tnn_content(context["last_tnn"])["addr"]
            stop_on_false = True
        elif reason in ("box_extrude_err", "extruder_extrude_err", "filament_err",
                        "empty_print", "macro_box_extrude_err", "macro_extruder_extrude_err"):
            if context["tnn"] is not None:
                address = box.box_state.get_Tnn_content(context["tnn"])["addr"]
        elif reason == "state_err":
            address = context["addr"]
        if address is not None:
            result = call(box.communication_set_box_mode, address, "IDLE")
            if stop_on_false and result is False:
                return False
        call(self.clear_err)

    def cmd_error_clear(self, gcmd):
        self.error_clear()

    def cmd_Tnn_retry_process(self, gcmd):
        self.Tnn_retry_process()

    def Tnn_retry_process(self):
        box = self.box
        call = self._calls()
        if box.error is not None:
            call(box.gcode.respond_info, "Tnn_retry_process: %s" % box.error)
        logging.info("error: %s, tnn: %s, last_tnn: %s, addr: %s", box.error,
                     box.error_tnn["tnn"], box.error_tnn["last_tnn"], box.error_tnn["addr"])
        if call(box.if_in_resume):
            return
        if box.last_err and "key847" not in box.last_err:
            box.last_err = None
        if box.material_auto_refill_flag:
            box.material_auto_refill_flag = False
        address = box.error_tnn.get("addr")
        if address is not None:
            call(box.wait_box_preloading, addr=address)
        reason = box.error
        if reason == "cut_err":
            virtual = box.error_tnn["vtnn"]
            call(self.clear_err)
            call(box.Tn_action, virtual)
        elif reason in _MACRO_ERRORS:
            call(self.macro_err_retry_process)
        elif reason in ("retrude_err", "box_extrude_err", "extruder_extrude_err",
                        "flush_err", "filament_err", "empty_print", "print_end_err"):
            if reason == "filament_err":
                call(box.gcode.run_script_from_command, "WAIT_EXTRUSION_ALL_MATERIALS")
            elif reason == "empty_print":
                call(box.motion.go_to_extrude_pos)
            call(getattr(self, reason + "_retry_process"))
        # The stored error, rather than a leaf's Boolean result, selects this tail.
        call(box.do_after_extrude)
        if box.error is None:
            if box.print_stats.state == "paused":
                call(box.gcode.run_script_from_command, "RESUME")
        else:
            call(box.set_cool_temp)

    def print_end_err_retry_process(self):
        box, call = self.box, self._calls()
        source = box.error_tnn["last_tnn"]
        if source:
            address = box.box_state.get_Tnn_content(source)["addr"]
            if call(box.communication_set_box_mode, address, "IDLE") is False:
                return False
        call(self.clear_err)
        call(box.gcode.run_script_from_command, "END_PRINT")

    def macro_err_retry_process(self):
        box, call = self.box, self._calls()
        commands = list(box.error_next_macro)
        source, target, address = (box.error_tnn[key] for key in ("last_tnn", "tnn", "addr"))
        tool = target or source
        if tool:
            address = box.box_state.get_Tnn_content(tool)["addr"]
        if address:
            if call(box.communication_set_box_mode, address, "IDLE") is False:
                return False
        call(self.clear_err)
        for command in commands:
            call(box.gcode.run_script_from_command, command)
        call(box.gcode.run_script_from_command, "M400")

    def empty_print_retry_process(self):
        box, call = self.box, self._calls()
        target = box.error_tnn["tnn"]
        if target is None:
            call(self.clear_err)
            return
        content = box.box_state.get_Tnn_content(target)
        call(box.communication_set_box_mode, content["addr"], "IDLE")
        call(box.communication_set_box_mode, content["addr"], "PRINT", num=content["num"])
        call(self.clear_err)
        temperature = call(box.get_material_target_temp, target)
        call(box.quickly_wait_heating, temperature)

    def _stage(self, call, source, target, reason, function, *args):
        if call(function, *args) is False:
            call(self.box.lifecycle.record_error, reason, {"last_tnn": source, "tnn": target})
            return False
        return True

    def flush_err_retry_process(self):
        box, call = self.box, self._calls()
        call(box.motion.go_to_extrude_pos)
        source, target = box.error_tnn["last_tnn"], box.error_tnn["tnn"]
        # Lookup precedes the both-None branch in the retained contract.
        content = box.box_state.get_Tnn_content(target)
        call(box.communication_set_box_mode, content["addr"], "IDLE")
        call(box.communication_set_box_mode, content["addr"], "PRINT", num=content["num"])
        call(self.clear_err)
        if source is None and target is None:
            return False
        if not self._stage(call, source, target, "flush_err", box.material_change_flush, source, target):
            return False
        call(box.motion.z_restore)
        call(box.gcode.run_script_from_command, "G92 E0")
        call(box.gcode.run_script_from_command, "G90")
        call(box.motion.restore_printer_accel)
        return True

    def _material_retry(self, kind):
        box, call = self.box, self._calls()
        source, target = box.error_tnn["last_tnn"], box.error_tnn["tnn"]
        if target is None and (kind != "extruder" or source is None):
            return
        if kind == "retrude":
            address = (box.error_tnn["addr"] if source is None else
                       box.box_state.get_Tnn_content(source)["addr"])
        else:
            address = box.box_state.get_Tnn_content(target)["addr"]
        if call(box.communication_set_box_mode, address, "IDLE") is False:
            return False
        call(self.clear_err)
        if kind == "retrude":
            if call(box.get_filament_sensor_detect):
                call(box.move_to_cut)
                call(box.motion.go_to_extrude_pos)
            if not self._stage(call, source, target, "retrude_err", box.box_retrude_material):
                return False
        if kind == "box":
            call(box.motion.go_to_extrude_pos)
        if kind != "extruder":
            if not self._stage(call, source, target, "box_extrude_err", box.box_extrude_material, target):
                return False
        if kind == "box":
            call(box.motion.go_to_extrude_pos)
        if not self._stage(call, source, target, "extruder_extrude_err", box.extruder_extrude, target):
            return False
        if not self._stage(call, source, target, "flush_err", box.material_change_flush, source, target):
            return False
        for command in ("G92 E0", "G90", "M400"):
            call(box.gcode.run_script_from_command, command)
        return True

    def filament_err_retry_process(self):
        return self._material_retry("filament")

    def box_extrude_err_retry_process(self):
        return self._material_retry("box")

    def extruder_extrude_err_retry_process(self):
        return self._material_retry("extruder")

    def retrude_err_retry_process(self):
        return self._material_retry("retrude")
