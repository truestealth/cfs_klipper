"""Preload, direct feed and retract helpers through the retained transport."""

from .native import UnsupportedRequest, build_request, slot_mask


class Feeding:
    def __init__(self, box):
        self.box = box

    def _send(self, address, command, data, report_err=True, encoding=1):
        box = self.box
        guard = box.host_guard(address)
        result = box.send_native(build_request(address, command, data, encoding=encoding), report_err=report_err)
        guard()
        return result

    def communication_set_pre_loading(self, addr, action, num=15, force=False):
        box = self.box
        guard = box.host_guard(addr)
        if box.print_stats.state == "printing" or (box.print_stats.state == "pause" and not force):
            return None
        value = {"CLOSE": 0, "OPEN": 1, "RUN": 2, "TIGHT": 3}.get(action, action)
        if value >= 2:
            present = box.get_filament_sensor_detect()
            guard()
            if present:
                return None
        return self._send(addr, "SET_PRE_LOADING", bytes((num, value)))

    def communication_tighten_up_enable(self, addr, enable):
        return self._send(addr, "TIGHTEN_UP_ENABLE", bytes(({"ENABLE": 0, "DISABLE": 1}[enable],)))

    def communication_retrude_process(self, addr, num, trigger, report_err=True):
        return self._send(addr, "RETRUDE_PROCESS", bytes((slot_mask(num, allow_zero=True),
                          {"BUFFER": 0, "MATERIAL": 1}[trigger])), report_err)

    def communication_extrude_process(self, addr, num, trigger="", stage=2, report_err=True, extrude=None):
        if self.box.boxcfg.version == 0:
            raise UnsupportedRequest("typed encoding-0 feed is unresolved")
        return self._send(addr, "EXTRUDE_PROCESS", bytes((slot_mask(num), stage, extrude if extrude else 0)), report_err)

    def wait_box_preloading(self, addr):
        box = self.box
        guard = box.host_guard(addr)
        for count in range(1, 32):
            result = box.send_native(build_request(addr, "GET_BOX_STATE"))
            guard()
            if not result or box.box_state.get_box_mode(addr) != "1" or count > 30:
                return result
            box._reactor.pause(box._reactor.monotonic() + 1)
            guard()

    def make_material_loose(self):
        box = self.box
        guard = box.host_guard()
        if box.get_purge_in_prime_tower():
            guard()
            return
        guard()
        box.extrude_process_inside_retry_loose = True
        max_x = box.boxcfg.config.getsection("stepper_x").getfloat("position_max")
        safe_y, speed = box.motion._positions("safe_pos_y", "clean_velocity")
        x, y = max_x - 10, safe_y - 10
        box.extrude_process_inside_retry_loose = True
        for command in ("G90\n G0 Y%.2f F%.2f" % (safe_y, 5000), "M400",
                        "G90\n G0 X%.2f Y%.2f F%.2f" % (x, y, speed),
                        "G90\n G0 X%.2f Y%.2f F%.2f" % (x, y / 2, speed),
                        "G90\n G0 X%.2f Y%.2f F%.2f" % (x, y, speed)):
            box.gcode.run_script_from_command(command)
            guard()
        box.motion.go_to_extrude_pos()
        guard()
        box.extrude_process_inside_retry_loose = False

    def cmd_set_pre_loading(self, gcmd):
        box = self.box
        address = gcmd.get_int("ADDR", 0, minval=1, maxval=4)
        num = gcmd.get_int("NUM", 0, minval=0, maxval=15)
        params = gcmd.get_command_parameters()
        action = params.get("ACTION")
        power = params.get("POWER_ON")
        if action == "OPEN":
            box.preloading_en = True
        elif action == "CLOSE":
            box.preloading_en = False
        if power in ("ENABLE", "DISABLE"):
            box.preloading_poweron = power == "ENABLE"
        if action not in ("OPEN", "CLOSE", "RUN", "TIGHT"):
            return
        if action in ("OPEN", "CLOSE"):
            for addr in range(1, 5):
                if box.box_state.Tn_data["T%d" % addr]["state"] == "connect":
                    self.communication_set_pre_loading(addr, action, num)
        else:
            self.communication_set_pre_loading(address, action, num)

    def cmd_tighten_up_enable(self, gcmd):
        addr = gcmd.get_int("ADDR", minval=1, maxval=4)
        enable = gcmd.get_command_parameters().get("ENABLE")
        if enable in ("ENABLE", "DISABLE"):
            self.communication_tighten_up_enable(addr, enable)

    def cmd_retrude_process(self, gcmd):
        addr = gcmd.get_int("ADDR", minval=1, maxval=4)
        params = gcmd.get_command_parameters()
        self.communication_retrude_process(addr, params.get("NUM"), params.get("TRIGGER"))

    def cmd_extrude_2_process(self, gcmd):
        gcmd.get_int("ADDR", minval=1, maxval=4)
        params = gcmd.get_command_parameters()
        slot_mask(params.get("NUM"))
        {"CONNECTION": 0, "BUFFER": 1}[params.get("TRIGGER")]
        raise UnsupportedRequest("legacy opcode 13 has an unresolved host/MCU payload mismatch")

    def cmd_extrude_process(self, gcmd):
        addr = gcmd.get_int("ADDR", minval=1, maxval=9)
        params = gcmd.get_command_parameters()
        if self.box.boxcfg.version == 0:
            raise UnsupportedRequest("typed encoding-0 feed is unresolved")
        stage = gcmd.get_int("STAGE", 2, minval=1, maxval=9)
        self.communication_extrude_process(addr, params.get("NUM"), stage)
