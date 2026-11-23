"""Replay saved recovery phases without treating normal returns as completion."""

import logging
from dataclasses import dataclass
from typing import Callable, Optional

from .errors import _ERRORS


_MACRO_ERRORS = frozenset("macro_" + name for name in (
    "cut_err", "retrude_err", "box_extrude_err", "extruder_extrude_err", "flush_err"))
_LOAD_ERRORS = ("box_extrude_err", "filament_err", "extruder_extrude_err", "flush_err")


@dataclass
class PausedTool:
    job: object
    position: int
    logical: str
    source: Optional[str]
    target: str
    check_device: Callable[[], None]
    ready: bool = False


class Recovery:
    def __init__(self, box):
        self.box = box
        self.paused_tool = None

    def _calls(self, check=None):
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
        if check is not None:
            checks.append(check)

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

    def save_paused_tool(self, job, position, logical):
        box = self.box
        target = box.error_tnn["tnn"]
        if (job is None or position is None or job is not box.virtual_sdcard.current_file
                or job.closed or box.error not in _LOAD_ERRORS
                or target is None or box.box_state.Tnn_map.get(logical) != target):
            return
        address = box.box_state.get_Tnn_content(target)["addr"]
        # NOTE: virtual SD advances past the failed tool command on pause.
        # Clearing the error must leave this job's incomplete change pending.
        self.paused_tool = PausedTool(job, position, logical, box.error_tnn["last_tnn"],
                                     target, box.lifecycle._report_guard(address, operation=False))

    def _current_paused_tool(self):
        pending = self.paused_tool
        if pending is not None and (pending.job is not self.box.virtual_sdcard.current_file
                                    or pending.job.closed):
            self.paused_tool = pending = None
        return pending

    def _check_paused_tool(self, pending):
        box = self.box
        pending.check_device()
        if (self._current_paused_tool() is not pending or box.print_stats.state != "paused"
                or box.virtual_sdcard.file_position != pending.position
                or box.box_state.Tnn_map.get(pending.logical) != pending.target):
            raise RuntimeError("CFS paused tool no longer matches the job position or mapping")

    def _head_has_target(self, target):
        box = self.box
        return (box.last_cmd == target and box.has_actual_head_sensor()
                and box.get_filament_sensor_detect() is True)

    def confirm_paused_resume(self):
        pending = self._current_paused_tool()
        if pending is None:
            return False
        self._check_paused_tool(pending)
        if not pending.ready or not self._head_has_target(pending.target):
            raise RuntimeError("CFS %s is pending. Insert filament and press Retry." % pending.target)
        return True

    def _retry_paused_tool(self, pending):
        box = self.box
        check = lambda: self._check_paused_tool(pending)
        call = self._calls(check)
        if call(box.if_in_resume):
            return False
        if not box.has_actual_head_sensor() or set(box.motion._homed(check)) != set("xyz"):
            raise RuntimeError("CFS retry requires a head filament sensor and homed XYZ")
        target = pending.target
        if not call(box.is_material_available, target):
            call(box.gcode.respond_info, "CFS %s is empty. Insert filament and press Retry." % target)
            return False
        head_present = call(box.get_filament_sensor_detect)
        if box.last_cmd not in (None, target) or (head_present and box.last_cmd != target):
            raise RuntimeError("CFS retry cannot identify the filament already in the head")
        if box.error is not None and (box.error not in _LOAD_ERRORS or box.error_tnn["tnn"] != target):
            raise RuntimeError("CFS retry is blocked by another error")
        if not (pending.ready and head_present):
            pending.ready = False
            kind = "extruder" if head_present else "filament"
            if self._material_retry(kind, (pending.source, target), check) is not True:
                return False
            if box.error is not None:
                return False
            call(box.do_after_extrude)
            call(box.gcode.run_script_from_command, "M400")
            if not call(self._head_has_target, target):
                call(box.gcode.respond_info, "CFS %s lost head-sensor confirmation." % target)
                return False
            pending.ready = True
        if box.error is not None:
            return False
        check()
        # RESUME rechecks the head in BOX_RESUME_EXTRUDE before moving back.
        box.gcode.run_script_from_command("RESUME")
        self.paused_tool = None
        return True

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
        try:
            self.Tnn_retry_process()
        except (OSError, ValueError, RuntimeError) as error:
            raise gcmd.error(str(error)) from error

    def Tnn_retry_process(self):
        box = self.box
        had_pending = self.paused_tool is not None
        pending = self._current_paused_tool()
        if pending is not None:
            return self._retry_paused_tool(pending)
        if had_pending:
            return False
        if (box.error is None and getattr(box.lifecycle, "passive", None) is False
                and getattr(getattr(box, "virtual_sdcard", None), "current_file", None) is not None):
            return False
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

    def _material_retry(self, kind, tools=None, check=None):
        box, call = self.box, self._calls(check)
        source, target = box.error_tnn["last_tnn"], box.error_tnn["tnn"]
        if tools is not None:
            source, target = tools
        if target is None and (kind != "extruder" or source is None):
            return
        if kind == "retrude":
            address = (box.error_tnn["addr"] if source is None else
                       box.box_state.get_Tnn_content(source)["addr"])
        else:
            address = box.box_state.get_Tnn_content(target)["addr"]
        if tools is not None and kind == "extruder":
            mode = call(box.communication_set_box_mode, address, "PRINT",
                        num=box.box_state.get_Tnn_content(target)["num"])
        else:
            mode = call(box.communication_set_box_mode, address, "IDLE")
        if mode is False:
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
        if tools is not None and not call(self._head_has_target, target):
            call(box.gcode.respond_info, "CFS %s did not reach the head sensor." % target)
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
