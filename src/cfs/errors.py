"""Shared CFS error reporting and saved recovery context."""

import logging


# Client-visible text and formatting are part of the retained interface.
_MESSAGES = {
    "key_timeout": (831, "serial_485 communication timeout", "address"),
    "key_params_err": (834, "params error, send data: %s", "data_send"),
    "key_extrude_err1": (835, "extrude error, maybe it's blocked at the connections", "slot"),
    "key_extrude_err4": (838, "extrude error, through the connections but not extrude", "slot"),
    "key_filament_err": (839, "filament error, no filament detected at box extrude position", "slot"),
    "key_state_err": (840, "box switch state error, check it's current action, addr: %d, cmd: 0x%x", "state"),
    "key_rfid_err": (843, "rfid is error, get rfid: %s", "rfid"),
    "key_joint_err": (844, "the pneumatic joint is abnormal and may collapse", None),
    "key_speed_err": (846, "empty printing, box speed is smaller than extruder", None),
    "key_enwind_err": (847, "empty printing, material enwind", None),
    "key_retrude_err1": (849, "retrude error, failed to exit connections", "slot"),
    "key_retrude_err2": (850, "retrude error, unspecified retrude, multiple connections triggered, addr: %d", "in_address"),
    "key_load_err": (857, "motor load error", None),
}
_SPECIAL = tuple("key%d" % n for n in (843, 853, 858, 859, 860, 861, 862))
_ERRORS = frozenset((
    "cut_err", "retrude_err", "box_extrude_err", "extruder_extrude_err",
    "state_err", "filament_err", "empty_print", "flush_err", "print_end_err",
    "e_err_cut_err", "z_err_extrusion_all_materials_err",
    "macro_extrusion_all_materials_err", "print_end_move_to_cut_err",
    "e_err_tn_extrude_err", "macro_cut_err", "macro_retrude_err",
    "macro_box_extrude_err", "macro_extruder_extrude_err", "macro_flush_err",
))


def format_error(key, facts):
    """Format a decoded sender error using the original client schema."""
    code, message, kind = _MESSAGES[key]
    values = ""
    if kind == "address":
        values = str(facts["address"])
    elif kind == "data_send":
        message %= facts["data_send"]
    elif kind == "state":
        message %= (facts["address"], facts["command"])
    elif kind == "in_address":
        message %= facts["address"]
    elif kind in ("slot", "rfid"):
        slot = facts.get("slot")
        if slot is None:
            # Sender errors use the request's first data byte as a slot mask.
            mask = facts["data_send"][4]
            slot = {0: "0", 1: "A", 2: "B", 4: "C", 8: "D", 15: "0"}[mask]
        if kind == "rfid":
            message %= facts["rfid"]
        values = '%d, "%s"' % (facts["request_address"], slot)
    return '!! {"code":"key%d", "msg":"%s", "values": [%s]}' % (code, message, values)


class ErrorHandler:
    """Operate on the same state observed by retained Klipper consumers."""

    def __init__(self, box, gcode, reactor, print_stats, virtual_sdcard, current_dispatch):
        self.box = box
        self.gcode = gcode
        self.reactor = reactor
        self.print_stats = print_stats
        self.virtual_sdcard = virtual_sdcard
        self.current_dispatch = current_dispatch

    def report_error(self, key, facts, check_context=None):
        self.report_message(format_error(key, facts), self.box.print_state, check_context)

    def report_message(self, message, print_state, check_context=None):
        box = self.box

        def perform(function, *args):
            if check_context is not None:
                check_context()
            result = function(*args)
            if check_context is not None:
                check_context()
            return result

        box.inside_error = True
        try:
            if box.last_err == message:
                logging.info("the same err, last_err: %s", message)
                return
            logging.info("%s", message)
            perform(self.gcode.respond_raw, message)
            box.last_err = message
            if any(key in message for key in _SPECIAL):
                return
            dispatch = perform(self.current_dispatch)
            if print_state == "printing":
                perform(self.gcode.run_script_from_command, "PAUSE POST_WORK=0")
                perform(self.gcode.run_script_from_command, "M400")
                while (self.print_stats.state != "paused" and
                       dispatch != self.virtual_sdcard.work_handler_dispatch):
                    perform(self.reactor.pause, self.reactor.monotonic() + 1)
            if box.move_z is not None:
                move_z = box.move_z
                box.move_z = None
                perform(box.record_tn_move_z, 0)
                for command in ("BOX_MOVE_TO_SAFE_POS", "M400", "G91",
                                "G0 Z-%.2f F600" % move_z, "G90"):
                    perform(self.gcode.run_script_from_command, command)
            if box.max_velocity and box.max_accel:
                perform(self.gcode.run_script_from_command, "G0 F%.2f" % box.max_velocity)
                perform(self.gcode.run_script_from_command, "M204 S%d" % box.max_accel)
                box.max_velocity = box.max_accel = None
            if print_state == "printing":
                perform(self.gcode.run_script_from_command, "SAVE_GCODE_STATE NAME=PAUSE_STATE")
                perform(self.gcode.run_script_from_command, "DO_AFTER_PAUSE")
        except Exception:
            logging.exception("An exception occurred while reporting a CFS error")
        finally:
            box.inside_error = False

    def record_error(self, reason, context):
        box = self.box
        if reason not in _ERRORS:
            logging.warning("Unknown CFS error: %s", reason)
            return
        if box.error is not None and not context.get("cover", False):
            logging.warning("CFS error already recorded: %s", box.error)
            return
        box.error = reason
        for key in ("last_tnn", "tnn", "addr", "vtnn"):
            if context.get(key):
                box.error_tnn[key] = context[key]
        if reason == "filament_err" and context.get("filament_sensor_enable", True):
            box.set_head_runout_enabled(True)
