"""Box contract components; the bootstrap still refuses activation.

Explicit construction supports PC checks of state, settings and storage. It
does not obtain a printer or a serial object, start timers, or run mechanics.
"""

from copy import deepcopy
import importlib
import logging
from math import isfinite
from itertools import zip_longest

from .configuration import BoxConfiguration, _field_name
from .cutting import CutSensor, Cutter
from .extrusion import Extrusion
from .firmware import DeviceVersions
from .feeding import Feeding
from .loading import Loading
from .retraction import Retraction
from .toolchange import ToolChange
from .printing import Printing
from .finishing import Finishing
from .events import SlotEvents
from .runout import Runout
from .recovery import Recovery
from .resume import Resume
from .errors import format_error
from .lifecycle import Lifecycle
from .motion import Motion
from .native import NativeSerial, Request, UnsupportedRequest, build_request, diagnostic_request, slot_mask
from .operations import OperationContext
from .protocol import DecodeContext, decode_reply
from .state import BoxState
from .storage import SpoolStore


# These labels are part of the existing diagnostic presentation, not live data.
COLOR_NAMES = {
    "F4E076": "浅黄", "FF614B": "橙红", "6C84FF": "钴蓝", "9CFF4F": "草绿",
    "BA552A": "褐色", "FF1E1E": "大红", "FFFFFF": "白色", "9EA7AE": "灰色",
    "CE58F8": "紫色", "26EEEE": "天蓝", "66DBA2": "浅绿", "FF97E1": "嫩粉红",
    "FF8B1F": "桔黄", "1B04AE": "深蓝", "000000": "黑色", "7A92AC": "深灰",
    "00A3FF": "蓝色", "72A530": "橄榄绿", "3DDF57": "中绿", "FF37AF": "粉色",
    "FFF014": "柠檬黄", "B2A1E1": "淡紫色", "8A43FF": "蓝紫", "FFA800": "中黄",
}


class MissingProtocolEffect(RuntimeError):
    """A decoded action needs an operation/lifecycle handler not yet bound."""


class StaleReplyError(ValueError):
    """Identity or binding changed while the borrowed API was waiting."""


class Box:
    """Compose implemented data behavior without activating an unfinished box.

    ``base_dir`` is supplied by the caller; PC tests always use a temporary
    directory. Lifecycle and host actions are wired explicitly on substitutes
    until the complete replacement is ready for printer acceptance.
    """

    def __init__(self, config, base_dir, config_paths=None):
        self.boxcfg = BoxConfiguration(config)
        self.store = SpoolStore(base_dir)
        self.box_state = BoxState(self.store)
        self.config_paths = config_paths
        self.find_cut_pos = 0.0
        self.t_command = ""
        self.custom_command_result = ""

        # Retained consumers use these paths. The aliases share live state;
        # they do not replicate the original private class hierarchy.
        self.box_action = self
        self.box_save = self
        self.last_cmd = None
        self.last_err = None
        self.move_z = None
        self.max_velocity = self.max_accel = None
        self.preloading_en = None
        self.preloading_poweron = None
        self.inside_error = False
        self.heart_process_enable = [True] * 4
        self.native = None
        self.device_versions = DeviceVersions()
        self._reactor = None
        self._binding_generation = 0
        self._effect_handlers = {}
        self.last_replies = {}
        self.state_extensions = {}
        self.extrude_process_state = None
        self.error = None
        self.error_tnn = dict.fromkeys(("last_tnn", "tnn", "addr", "vtnn"))
        self.box_filament_err = False
        self.is_use_ending_material = False
        self.inside_extrusion_all_materials = False
        self.print_state = ""
        self.filament_error_message = None
        self.last_cmd_tmp = None
        self.lifecycle = None
        self._printer = None
        self.slot_events = {}
        self.operations = OperationContext(self)
        self.speed_factor = None
        self.start_print_retraction_distance = 0
        self.error_next_macro = []
        self._ffi_pair = None
        self.motion = Motion(self)
        self.extrusion = Extrusion(self)
        self.cut_sensor = self.cutter = None
        self.feeding = Feeding(self)
        self.loading = Loading(self)
        self.retraction = Retraction(self)
        self.toolchange = ToolChange(self)
        self.printing = Printing(self)
        self.finishing = Finishing(self)
        self.events = SlotEvents(self)
        self.runout = Runout(self)
        self.recovery = Recovery(self)
        self.resume = Resume(self)
        self.flushing_sign = str(self.store.path.parent.parent / "config" / "flushing_sign")
        self.material_auto_refill_flag = False
        self.inside_auto_refill = False

    def host_guard(self, address=None):
        """Capture ownership once for a host action's cooperative calls."""
        if self.lifecycle is None or not self.lifecycle.running:
            raise RuntimeError("the CFS host lifecycle is not ready")
        guard = self.lifecycle._report_guard(address)
        guard()
        return guard

    def get_filament_sensor_detect(self):
        guard = self.host_guard()
        value = self.lifecycle.head_present()
        guard()
        return value

    def has_actual_head_sensor(self):
        return self.lifecycle is not None and getattr(self.lifecycle, "head_sensor", None) is not None

    def if_in_resume(self):
        guard = self.host_guard()
        virtual_sd = self._printer.lookup_object("virtual_sdcard")
        guard()
        value = virtual_sd.do_resume_status
        logging.info("virtual-SD resume status: %s", value)
        return value

    def set_flushing_sign(self):
        guard = self.host_guard()
        self.gcode.run_script_from_command("M400")
        guard()

    def reset_flushing_sign(self):
        self.set_flushing_sign()

    def _material_type(self, tnn):
        if tnn is None:
            return None
        content = self.box_state.get_Tnn_content(tnn)
        return self.box_state.get_Tn_data(content["addr"], "material_type", content["num"])

    def get_material_target_temp(self, tnn):
        try:
            return self.store.get_material_temperature(self._material_type(tnn), self.boxcfg.Tn_extrude_temp)
        except Exception:
            logging.exception("Could not read CFS material temperature")
            return self.boxcfg.Tn_extrude_temp

    def get_material_max_extrusion_speed(self, tnn):
        try:
            return self.store.get_material_max_speed(self._material_type(tnn))
        except Exception:
            logging.exception("Could not read CFS material extrusion speed")
            return None

    def _host_ffi(self):
        if self._ffi_pair is None:
            guard = self.host_guard()
            pair = importlib.import_module("chelper").get_ffi()
            guard()
            self._ffi_pair = pair
        return self._ffi_pair

    @property
    def ffi_main(self):
        return self._host_ffi()[0]

    @property
    def ffi_lib(self):
        return self._host_ffi()[1]

    def read_measuring_wheel(self, address):
        """Return a fresh ordinary wheel sample; a retained cache is not one."""
        guard = self.host_guard()
        result = self.send_native(build_request(address, "MEASURING_WHEEL", b"\x01"))
        guard()
        frame = self.last_replies.get(address)
        if result is not True or frame is None or frame.command != 0x0E or frame.status != 0:
            return False, None
        value = self.box_state.get_Tn_data(address, "measuring_wheel")
        if not isinstance(value, (int, float)) or not isfinite(value):
            return False, None
        return True, value

    def Tn_Extrude(self, extrude, velocity, temp, percent=None, tnn=None, error_report=True):
        return self.extrusion.Tn_Extrude(extrude, velocity, temp, percent, tnn, error_report)

    def cal_flush_list(self, source, result):
        self.extrusion.cal_flush_list(source, result)

    def get_flush_len(self, source, target):
        return self.extrusion.get_flush_len(source, target)

    def get_flush_velocity(self, source, target):
        return self.extrusion.get_flush_velocity(source, target)

    def get_flush_temp(self, source, target):
        return self.extrusion.get_flush_temp(source, target)

    def quickly_wait_heating(self, temperature):
        self.extrusion.quickly_wait_heating(temperature)

    def nozzle_clean(self):
        self.cutter.nozzle_clean()

    def do_after_extrude(self):
        self.motion.do_after_extrude()

    def update_filament_pos(self, tnn, data=None):
        if tnn is None:
            logging.warning("Cannot update filament position without a source")
            return
        address = self.box_state.get_Tnn_content(tnn)["addr"]
        self.box_state.modify_Tn_data(address, "None" if data is None else data, "filament")

    def is_material_available(self, tnn):
        content = self.box_state.get_Tnn_content(tnn)
        address, slot = content["addr"], content["num"]
        guard = self.host_guard(address)
        previous = self.last_replies.get(address)
        result = self.send_native(build_request(address, "GET_FILAMENT_SENSOR_STATE", b"\x00"))
        guard()
        frame = self.last_replies.get(address)
        if (result is not True or frame is None or frame is previous
                or frame.status != 0 or frame.command != 8 or len(frame.data) != 1):
            raise RuntimeError("fresh ordinary material-sensor observation required")
        mask = self.box_state.get_Tn_inner_data(address, "filament_sensor", subpart="material")
        return bool(mask & slot_mask(slot))

    def get_purge_in_prime_tower(self):
        """Read the retained slicer policy; unavailable metadata returns None."""
        guard = self.host_guard()
        try:
            virtual_sd = self._printer.lookup_object("virtual_sdcard")
            guard()
            stats = self._printer.lookup_object("print_stats")
            guard()
            metadata = virtual_sd.gcode_metadata.get("metadata", {})
            version = metadata.get("slicer_version", "")
            supported = False
            if metadata.get("slicer", "").upper() == "CREALITY" and version:
                parts = [int(part) for part in version.split(".")]
                supported = True
                for current, minimum in zip_longest(parts, (5, 1, 7, 10471), fillvalue=0):
                    if current != minimum:
                        supported = current > minimum
                        break
            if (stats.state == "printing" and virtual_sd.gcode_metadata
                    and virtual_sd.first_tnn_finished and supported):
                parameters = virtual_sd.get_gcode_flush_para()
                guard()
                return parameters.get("purge_in_prime_tower", 0) if parameters else None
        except Exception:
            guard()
            logging.exception("Could not read the retained prime-tower policy")
        return None

    def communication_set_pre_loading(self, addr, action, num=15, force=False):
        return self.feeding.communication_set_pre_loading(addr, action, num, force)

    def communication_tighten_up_enable(self, addr, enable):
        return self.feeding.communication_tighten_up_enable(addr, enable)

    def communication_set_box_mode(self, addr, mode, num="0"):
        guard = self.host_guard(addr)
        value = {"PRINT": 0, "IDLE": 1}[mode]
        result = self.send_native(build_request(addr, "SET_BOX_MODE", bytes((slot_mask(num, allow_zero=True), value))))
        guard()
        return result

    def communication_extrude_process(self, addr, num, trigger="", stage=2, report_err=True, extrude=None):
        return self.feeding.communication_extrude_process(addr, num, trigger, stage, report_err, extrude)

    def communication_retrude_process(self, addr, num, trigger, report_err=True):
        return self.feeding.communication_retrude_process(addr, num, trigger, report_err)

    def wait_box_preloading(self, addr):
        return self.feeding.wait_box_preloading(addr)

    def make_material_loose(self):
        return self.feeding.make_material_loose()

    def box_extrude_material(self, tnn):
        if tnn is not None:
            self.motion._positions("safe_pos_y")
            if self.boxcfg.version != 1:
                raise UnsupportedRequest("staged loading requires the supported box encoding 1")
        return self.loading.box_extrude_material(tnn)

    def box_retrude_material(self, report_error=True):
        return self.retraction.box_retrude_material(report_error)

    def move_to_cut(self, report_error=True):
        return self.cutter.move_to_cut(report_error)

    def go_to_extrude_pos(self):
        return self.motion.go_to_extrude_pos()

    def Tn_action(self, vtnn):
        return self.toolchange.Tn_action(vtnn)

    def extruder_extrude(self, tnn):
        return self.loading.extruder_extrude(tnn)

    def material_change_flush(self, source, target, error_report=True):
        return self.finishing.material_change_flush(source, target, error_report)

    def is_use_ending_material_flush(self, err, vtnn):
        return self.finishing.is_use_ending_material_flush(err=err, vtnn=vtnn)

    def record_tn_cur_tnn(self, tnn):
        self.printing.record_tn_cur_tnn(tnn)

    def has_flushing_sign(self):
        return self.printing.has_flushing_sign()

    def clear_err(self):
        self.recovery.clear_err()

    def error_clear(self):
        return self.recovery.error_clear()

    def Tnn_retry_process(self):
        return self.recovery.Tnn_retry_process()

    def extrusion_all_materials(self):
        return self.runout.extrusion_all_materials()

    def check_material_refill(self):
        return self.runout.check_material_refill()

    def check_connect(self, address):
        return self.runout.check_connect(address)

    def communication_get_box_state(self, address):
        return self.runout.communication_get_box_state(address)

    def update_Tnn_map(self):
        return self.runout.update_Tnn_map()

    def set_cool_temp(self):
        return self.runout.set_cool_temp()

    def power_loss_restore(self):
        return self.resume.power_loss_restore()

    def startup_refresh(self, address):
        guard = self.host_guard(address)
        self.communication_set_pre_loading(address, 2)
        guard()
        for command in ("GET_RFID", "GET_REMAIN_LEN"):
            self.send_native(build_request(address, command, b"\x0f"))
            guard()

    def check_and_extrude(self, extrude, velocity, wait=True):
        guard = self.host_guard()
        if self.box_state.extrude_err:
            return False
        for command in ("G91", "G0 E%.2f F%.2f" % (extrude, velocity), "G90"):
            self.gcode.run_script_from_command(command)
            guard()
        if wait:
            self.gcode.run_script_from_command("M400")
            guard()
        return not self.box_state.extrude_err

    def register_lifecycle(self, printer, **kwargs):
        """Wire functional Klipper hooks explicitly; bootstrap activation is gated."""
        if self.lifecycle is not None:
            raise RuntimeError("the CFS lifecycle is already registered")
        lifecycle = Lifecycle(self, printer, **kwargs)
        lifecycle.register()
        self.lifecycle, self._printer = lifecycle, printer
        return lifecycle

    def register_host_commands(self, printer):
        """Register real host actions before retained macro aliases are loaded."""
        self.cut_sensor = CutSensor(self.boxcfg.config, self.boxcfg.switch_pin, printer=printer)
        self.cutter = Cutter(self, self.motion, self.cut_sensor)
        for name, method in Motion.COMMANDS.items():
            self.gcode.register_command(name, getattr(self.motion, method))
        for name, handler in (
            ("BOX_TN_EXTRUDE", self.extrusion.cmd_Tn_Extrude),
            ("BOX_GET_FLUSH_LEN", self.extrusion.cmd_get_flush_len),
            ("BOX_MATERIAL_FLUSH", self.extrusion.cmd_material_flush),
            ("BOX_GENERATE_FLUSH_ARRAY", self.extrusion.cmd_generate_flush_array),
            ("BOX_GET_FLUSH_VELOCITY_TEST", self.extrusion.cmd_get_flush_velocity_test),
            ("BOX_MOVE_TO_CUT", self.cmd_move_to_cut),
            ("BOX_CUT_STATE", self.cmd_cut_state),
            ("BOX_CUT_MATERIAL", self.cmd_cut_material),
            ("BOX_FIND_CUT_POS", self.cmd_find_cut_pos),
        ):
            self.gcode.register_command(name, handler)
        clean = self.cmd_nozzle_clean
        self.gcode.register_command("BOX_NOZZLE_CLEAN", clean)
        self.gcode.register_command("TEST_BOX_CLEAN", clean)
        if self.boxcfg.switch_pin:
            self.gcode.register_command("BOX_CUT_HALL_ZERO", self.cmd_cut_hall_zero)
            self.gcode.register_command("BOX_CUT_HALL_TEST", self.cmd_cut_hall_test)
        for name, handler in (
            ("BOX_SET_PRE_LOADING", self.feeding.cmd_set_pre_loading),
            ("BOX_TIGHTEN_UP_ENABLE", self.feeding.cmd_tighten_up_enable),
            ("BOX_RETRUDE_PROCESS", self.feeding.cmd_retrude_process),
            ("BOX_EXTRUDE_PROCESS", self.feeding.cmd_extrude_process),
            ("BOX_EXTRUDE_2_PROCESS", self.cmd_extrude_2_process),
            ("BOX_EXTRUDE_MATERIAL", self.cmd_box_extrude_material),
            ("BOX_EXTRUDER_EXTRUDE", self.cmd_extruder_extrude),
            ("BOX_RETRUDE_MATERIAL", self.cmd_box_retrude_material),
            ("BOX_RETRUDE_MATERIAL_WITH_TNN", self.cmd_retrude_material_with_tnn),
        ):
            self.gcode.register_command(name, handler)
        for name, handler in (
            ("BOX_START_PRINT", self.printing.cmd_box_start_print),
            ("BOX_END_PRINT", self.printing.cmd_box_end_print),
            ("BOX_START_PRINT_EXTRUDE_MATERIAL", self.printing.cmd_box_start_print_extrude_material),
            ("BOX_END", self.finishing.cmd_box_end),
            ("BOX_MATERIAL_CHANGE_FLUSH", self.finishing.cmd_material_change_flush),
            ("SET_BOX_MODE_PRINT_WHEN_NOZZLE_CLEAR", self.finishing.cmd_set_box_mode_print_when_nozzle_clear),
            ("BOX_CHECK_MATERIAL_REFILL", self.runout.cmd_check_material_refill),
            ("BOX_EXTRUSION_ALL_MATERIALS", self.runout.cmd_extrusion_all_materials),
            ("WAIT_EXTRUSION_ALL_MATERIALS", self.runout.cmd_wait_extrusion_all_materials),
            ("BOX_ERROR_CLEAR", self.recovery.cmd_error_clear),
            ("BOX_TNN_RETRY_PROCESS", self.recovery.cmd_Tnn_retry_process),
            ("BOX_ADD_TNN", self.resume.cmd_add_tnn),
            ("BOX_POWER_LOSS_RESTORE", self.resume.cmd_power_loss_restore),
            ("BOX_RESUME_EXTRUDE", self.resume.cmd_box_resume_extrude),
        ):
            self.gcode.register_command(name, handler)
        for name in self.box_state.Tnn_map:
            self.gcode.register_command(name, self._tool_handler(name, virtual=False))
        for name in self.box_state.Tn_map:
            self.gcode.register_command(name, self._tool_handler(name, virtual=True))

    def _tool_handler(self, name, virtual):
        def command(gcmd):
            self.t_command = name
            logical = self.box_state.Tn_map[name] if virtual else name
            self.Tn_action(logical)
        return command

    def cmd_extrude_2_process(self, gcmd):
        try:
            self.feeding.cmd_extrude_2_process(gcmd)
        except (ValueError, RuntimeError) as error:
            raise gcmd.error("cfs: %s" % error) from error

    def _cmd_material_phase(self, gcmd, action, reason):
        guard = self.lifecycle._report_guard(operation=False)
        guard()
        if self.if_in_resume():
            logging.warning("In resume, cannot run the CFS material phase")
            return
        if self.error is None:
            tnn = gcmd.get_command_parameters().get("TNN")
            if tnn is not None and tnn not in self.box_state.Tnn_map:
                logging.warning("Invalid physical CFS target: %s", tnn)
                return
            result = action(tnn)
            guard()
            if result:
                return
            self.lifecycle.record_error(reason, {"tnn": tnn})
            guard()
        self.error_next_macro.append(gcmd.get_commandline())

    def cmd_box_extrude_material(self, gcmd):
        self._cmd_material_phase(gcmd, self.box_extrude_material, "macro_box_extrude_err")

    def cmd_extruder_extrude(self, gcmd):
        self._cmd_material_phase(gcmd, self.loading.extruder_extrude, "macro_extruder_extrude_err")

    def cmd_box_retrude_material(self, gcmd):
        guard = self.host_guard()
        if self.if_in_resume():
            logging.warning("In resume, cannot retract material now")
            return
        if self.error is None:
            result = self.retraction.box_retrude_material()
            guard()
            if result:
                return
            self.lifecycle.record_error("macro_retrude_err", {})
            guard()
        self.error_next_macro.append(gcmd.get_commandline())

    def cmd_retrude_material_with_tnn(self, gcmd):
        self.retraction.retrude_material_with_tnn(gcmd.get_command_parameters().get("TNN"))

    def cmd_cut_state(self, gcmd):
        self.cut_sensor.state()

    def cmd_move_to_cut(self, gcmd):
        self.cutter.move_to_cut()

    def cmd_nozzle_clean(self, gcmd):
        self.nozzle_clean()

    def cmd_find_cut_pos(self, gcmd):
        self.cutter.find_cut_pos()

    def cmd_cut_hall_zero(self, gcmd):
        self.cutter.hall_zero()

    def cmd_cut_hall_test(self, gcmd):
        self.cutter.hall_test()

    def cmd_cut_material(self, gcmd):
        guard = self.host_guard()
        if self.if_in_resume():
            logging.warning("In resume, can not cut material now")
            return
        guard()
        result = self.cutter.cut_material()
        guard()
        if not result:
            self.lifecycle.record_error("macro_cut_err", {})
            guard()
            self.error_next_macro.append(gcmd.get_commandline())

    def set_head_runout_enabled(self, enabled):
        name = self.boxcfg.filament_sensor
        if name is not None:
            self.gcode.run_script_from_command(
                "SET_FILAMENT_SENSOR SENSOR=%s ENABLE=%d" % (name, enabled))

    def record_tn_move_z(self, move_z):
        """Forward the retained print metadata hook; this does not move Z."""
        if self._printer is None:
            raise RuntimeError("CFS printer dependencies are not bound")
        virtual_sd = self._printer.lookup_object("virtual_sdcard")
        gcode_move = self._printer.lookup_object("gcode_move")
        try:
            gcode_move.recordPrintFileName(
                virtual_sd.print_file_name_path, virtual_sd.current_file.name,
                tn_move_z=move_z)
        except Exception:
            logging.exception("Could not record CFS Z metadata")

    def bind_native(self, printer, families=None, effect_handlers=None):
        """Borrow the configured object explicitly, without connecting it.

        Hardware families are externally confirmed context, not inferred from
        the version/SN reply. This method is used with substitutes until the
        Klipper lifecycle is implemented; load_config does not call it.
        """
        adapter = NativeSerial.from_printer(printer, self.boxcfg.bus)
        reactor = printer.get_reactor()
        handlers = dict(effect_handlers or {})
        if any(not isinstance(name, str) or not callable(function)
               for name, function in handlers.items()):
            raise ValueError("protocol effect handlers must be named callables")
        if set(handlers) & set(self._data_effect_handlers(None)):
            raise ValueError("protocol data handlers cannot be overridden")
        versions = DeviceVersions(families)
        if self.lifecycle is not None and self.lifecycle.running:
            self.lifecycle.shutdown()
        self.operations.cancel()
        self._binding_generation += 1
        self._printer = printer
        self.native, self._reactor = adapter, reactor
        self.device_versions = versions
        self._effect_handlers = handlers
        self.last_replies.clear()
        self.state_extensions.clear()
        self.slot_events.clear()
        for address in range(1, 5):
            self.box_state.modify_Tn_data(address, "-1", "version")
            self.box_state.modify_Tn_data(address, "-1", "sn")

    def unbind_native(self):
        """Invalidate own pending work without closing the borrowed transport."""
        self._binding_generation += 1
        self.operations.cancel()
        for address in range(1, 5):
            self.invalidate_device(address)
        self.native = None
        self._reactor = None
        self._effect_handlers.clear()
        self.last_replies.clear()
        self.state_extensions.clear()
        self.slot_events.clear()

    def invalidate_device(self, address, raw_reply=None):
        self.device_versions.invalidate(address, raw_reply=raw_reply)
        if self.operations.active and self.operations.address == address:
            self.operations.cancel()
        self.state_extensions.pop(address, None)
        self.slot_events.pop(address, None)
        self.box_state.modify_Tn_data(address, "-1", "version")
        self.box_state.modify_Tn_data(address, "-1", "sn")

    def _accept_identity(self, address, data, raw):
        previous = self.device_versions.generation(address)
        try:
            identity = self.device_versions.update(address, data, raw_reply=raw)
        except ValueError:
            self.state_extensions.pop(address, None)
            raise
        if self.device_versions.generation(address) != previous:
            self.state_extensions.pop(address, None)
            self.slot_events.pop(address, None)
            if self.operations.active and self.operations.address == address:
                self.operations.cancel()
        self.box_state.modify_Tn_data(address, identity.version, "version")
        self.box_state.modify_Tn_data(address, identity.sn, "sn")

    def _schedule_groups(self, request):
        binding = self._binding_generation
        generation = self.device_versions.generation(request.address)

        def update(eventtime):
            if (self.native is None or binding != self._binding_generation
                    or generation != self.device_versions.generation(request.address)):
                return
            self.box_state.update_same_material_list(self.store.get_material_name)

        if self.lifecycle is not None and self.lifecycle.running:
            self.lifecycle.schedule(update)
        else:
            self._reactor.register_callback(update)

    def _data_effect_handlers(self, request):
        return {
            "identity": self._accept_identity,
            "set_context": lambda name, value: setattr(self, name, value),
            "set_global": lambda field, value: self.box_state.modify_Tn_data(0, value, uppart=field),
            "set_slot": lambda address, slot, part, value: self.box_state.modify_Tn_data(address, value, part, slot),
            "error_address": lambda address: self.error_tnn.update(addr=address),
            "snapshot_extrude_status": lambda value: setattr(self, "extrude_process_state", value),
            "state_extension": lambda address, connections, tags: self.state_extensions.update({
                address: {"connection_mask": connections, "tag_predicate_mask": tags}}),
            "warning": lambda text: logging.warning("%s", text),
            "schedule_groups": lambda: self._schedule_groups(request),
        }

    def send_native(self, request, context=None, retries_en=None, report_err=True):
        """Execute one checked request and apply its ordered semantic effects.

        Contextual pause/error/refill actions must have real handlers. Missing
        handlers gate that route instead of silently accepting an ACK as a
        completed procedure. Normal observations are updated by the decoder.
        """
        if self.native is None:
            raise RuntimeError("the stock native object is not bound")
        if not isinstance(request, Request):
            raise ValueError("native calls require a built Request")
        if request.address not in range(1, 5):
            raise UnsupportedRequest("unaddressed diagnostics have no confirmed CFS context")
        address = request.address
        profile = self.device_versions.profile(address)
        if request.command not in (0x01, 0x14, 0x55):
            profile = self.device_versions.require_supported(address)
        binding = self._binding_generation
        generation = self.device_versions.generation(address)

        def check_context():
            if self.lifecycle is not None and self.lifecycle.running:
                self.lifecycle.guard_device(address)
            if (binding != self._binding_generation
                    or generation != self.device_versions.generation(address)):
                raise StaleReplyError("CFS context changed while awaiting a native reply")

        try:
            frame = self.native.exchange(request, retries_en=retries_en, check_context=check_context)
        except StaleReplyError:
            raise
        except (OSError, ValueError):
            # An old failure must not invalidate a device installed by a
            # callback while the retained native API was waiting.
            check_context()
            if request.command == 0x14:
                self.invalidate_device(address)
            raise
        check_context()
        self.last_replies[address] = frame
        if request.command == 0x14 and (frame is None or frame.status != 0):
            self.invalidate_device(address, raw_reply=frame.raw if frame else None)
            generation = self.device_versions.generation(address)
        if context is None:
            if self.lifecycle is not None and self.lifecycle.running:
                self.print_state = self.print_stats.state
                if frame is not None and frame.status == 0x50 and self.last_cmd:
                    self.filament_error_message = format_error("key_filament_err", {
                        "request_address": address, "slot": self.last_cmd[2],
                    })
            context = DecodeContext(
                report_err=report_err,
                last_cmd=self.last_cmd, error=self.error, last_err=self.last_err,
                print_state=self.print_state,
                box_filament_err=self.box_filament_err,
                is_use_ending_material=self.is_use_ending_material,
                filament_error_message=self.filament_error_message,
            )
        try:
            result = decode_reply(self.box_state, frame, request, profile=profile, context=context)
        except ValueError:
            check_context()
            if request.command == 0x14:
                self.invalidate_device(address, raw_reply=frame.raw if frame else None)
            raise
        handlers = self._data_effect_handlers(request)
        handlers.update(self._effect_handlers)
        missing = {effect.name for effect in result.effects} - handlers.keys()
        if missing:
            raise MissingProtocolEffect("unbound protocol effects: " + ", ".join(sorted(missing)))
        for effect in result.effects:
            check_context()
            effect_result = handlers[effect.name](*effect.args)
            if effect.name == "identity" and binding == self._binding_generation:
                # Accepting this request's own new identity legitimately
                # advances its generation; external changes never do.
                generation = self.device_versions.generation(address)
            elif (effect.name == "timeout_process" and self.lifecycle is not None
                  and handlers[effect.name] == self.lifecycle.timeout_process
                  and binding == self._binding_generation
                  and type(effect_result) is int and effect_result == generation + 1
                  and effect_result == self.device_versions.generation(address)):
                # This handler's own guarded disconnect is a normal False
                # outcome; a different binding/device change is still stale.
                generation = effect_result
            check_context()
        return result.value

    def register_native_commands(self, gcode):
        self.gcode = gcode
        for name, handler in (
            ("BOX_SEND_DATA", self.cmd_send_data),
            ("BOX_COMMUNICATION_TEST", self.cmd_communication_test),
            ("BOX_CTRL_CONNECTION_MOTOR_ACTION", self.cmd_ctrl_connection_motor_action),
            ("BOX_GET_FILAMENT_SENSOR_STATE", self.cmd_get_filament_sensor_state),
            ("BOX_SET_BOX_MODE", self.cmd_set_box_mode),
            ("BOX_GET_BUFFER_STATE", self.cmd_get_buffer_state),
            ("BOX_GET_RFID", self.cmd_get_rfid),
            ("BOX_GET_REMAIN_LEN", self.cmd_get_remain_len),
            ("BOX_GET_BOX_STATE", self.cmd_get_box_state),
            ("BOX_MEASURING_WHEEL", self.cmd_measuring_wheel),
            ("BOX_GET_VERSION_SN", self.cmd_get_version_sn),
            ("BOX_GET_FIVE_WAY_STATE", self.cmd_get_five_way_state),
        ):
            gcode.register_command(name, handler)

    def _native_command(self, gcmd, command, data=b"", timeout=None, address=None):
        if address is None:
            address = gcmd.get_int("ADDR", minval=1, maxval=4)
        try:
            request = build_request(address, command, data, timeout=timeout, encoding=self.boxcfg.version)
            return self.send_native(request)
        except (OSError, ValueError, RuntimeError) as error:
            raise gcmd.error("cfs: %s" % error) from error

    def cmd_send_data(self, gcmd):
        address = gcmd.get_int("ADDR", None, minval=1, maxval=4)
        state = gcmd.get_int("STATE", None, minval=0, maxval=255)
        command = gcmd.get_int("CMD", 10, minval=0, maxval=32)
        timeout = gcmd.get_int("TIMEOUT", 5, minval=1, maxval=120)
        raw = gcmd.get_command_parameters().get("DATA", "")
        if address is None:
            logging.info("addr is None")
            address = 0
        if state is None:
            self.gcode.respond_info("state is None")
            state = 0
        try:
            data = bytes(int(character) for character in raw)
            self.send_native(diagnostic_request(address, state, command, data, timeout))
        except (OSError, ValueError, RuntimeError) as error:
            raise gcmd.error("cfs: %s" % error) from error

    def cmd_communication_test(self, gcmd):
        count = gcmd.get_int("COUNT", 10, minval=0, maxval=1000000)
        timeout = gcmd.get_float("TIMEOUT", 0.1, minval=0, maxval=1)
        interval = gcmd.get_float("INTERVAL", 0.1, minval=0.01, maxval=1)
        address = gcmd.get_int("ADDR", minval=1, maxval=4)
        number = gcmd.get_int("NUM", minval=0, maxval=255)
        expected = (number + 1) & 255
        mismatches = timeouts = 0
        for index in range(count):
            self.gcode.respond_info("%d. addr[%d] test start" % (index, address))
            result = self._native_command(gcmd, "COMMUNICATION_TEST", bytes((number,)), timeout, address)
            if result is None:
                timeouts += 1
                self.gcode.respond_info("%d.timeout, TIMEOUT=%.2f, send data: 0x%x" % (timeouts, timeout, number))
            elif result != expected:
                mismatches += 1
                self.gcode.respond_info("%d.get return data(0x%x) error, send data: 0x%x" % (mismatches, result, number))
            self._reactor.pause(self._reactor.monotonic() + interval)
            self.gcode.respond_info("%d. addr[%d] test finish" % (index, address))

    def cmd_ctrl_connection_motor_action(self, gcmd):
        address = gcmd.get_int("ADDR", minval=1, maxval=4)
        actions = {"STOP": 0, "EXTRUDE": 1, "RETRUDE": 2}
        action = gcmd.get_command_parameters().get("ACTION")
        if action not in actions:
            logging.warning("param is error, ACTION only with 'STOP', 'EXTRUDE' and 'RETRUDE'")
            return
        self._native_command(gcmd, "CTRL_CONNECTION_MOTOR_ACTION", bytes((actions[action],)), address=address)

    def cmd_get_filament_sensor_state(self, gcmd):
        address = gcmd.get_int("ADDR", minval=1, maxval=4)
        positions = {"MATERIAL": 0, "CONNECTIONS": 1}
        position = gcmd.get_command_parameters().get("POSITION")
        if position not in positions:
            logging.warning("param is error, POSITION only with 'MATERIAL' and 'CONNECTIONS'")
            return
        self._native_command(gcmd, "GET_FILAMENT_SENSOR_STATE", bytes((positions[position],)), address=address)
        value = self.box_state.get_Tn_inner_data(address, "filament_sensor", subpart=position.lower())
        self.gcode.respond_info("[box] filament sensor state: %x" % value)

    def cmd_set_box_mode(self, gcmd):
        address = gcmd.get_int("ADDR", minval=1, maxval=4)
        modes = {"PRINT": 0, "IDLE": 1}
        parameters = gcmd.get_command_parameters()
        mode = parameters.get("MODE")
        if mode not in modes:
            logging.warning("param is error, MODE only with 'PRINT' and 'IDLE'")
            return
        try:
            mask = slot_mask(parameters.get("NUM", "0"), allow_zero=True)
        except ValueError as error:
            logging.warning("%s", error)
            return
        self._native_command(gcmd, "SET_BOX_MODE", bytes((mask, modes[mode])), address=address)

    def cmd_get_buffer_state(self, gcmd):
        self._native_command(gcmd, "GET_BUFFER_STATE")

    def cmd_get_rfid(self, gcmd):
        address = gcmd.get_int("ADDR", minval=1, maxval=4)
        mask = gcmd.get_int("NUM", minval=0, maxval=15)
        self._native_command(gcmd, "GET_RFID", bytes((mask,)), address=address)

    def cmd_get_remain_len(self, gcmd):
        address = gcmd.get_int("ADDR", minval=1, maxval=4)
        mask = gcmd.get_int("NUM", minval=0, maxval=15)
        self._native_command(gcmd, "GET_REMAIN_LEN", bytes((mask,)), address=address)

    def cmd_get_box_state(self, gcmd):
        self._native_command(gcmd, "GET_BOX_STATE")

    def cmd_measuring_wheel(self, gcmd):
        address = gcmd.get_int("ADDR", minval=1, maxval=4)
        actions = {"CLEAN": 0, "GET": 1}
        action = gcmd.get_command_parameters().get("ACTION")
        if action not in actions:
            logging.warning("param is error, ACTION only with 'GET' and 'CLEAN'")
            return
        self._native_command(gcmd, "MEASURING_WHEEL", bytes((actions[action],)), address=address)

    def cmd_get_version_sn(self, gcmd):
        self._native_command(gcmd, "GET_VERSION_SN")

    def cmd_create_connect(self, gcmd):
        address = gcmd.get_int("ADDR", minval=1, maxval=4)
        if self.lifecycle is None or not self.lifecycle.running:
            raise gcmd.error("cfs: the Klipper lifecycle is not ready")
        if self.print_stats.state != "printing":
            self._native_command(gcmd, "CREATE_CONNECT", address=address)

    def cmd_do_after_pause(self, gcmd):
        if self.lifecycle is None or not self.lifecycle.running:
            raise gcmd.error("cfs: the Klipper lifecycle is not ready")
        self.lifecycle.do_after_pause()

    def cmd_get_five_way_state(self, gcmd):
        for address in range(1, 5):
            name = "T%d" % address
            if self.box_state.Tn_data[name]["state"] != "connect":
                continue
            self._native_command(gcmd, "GET_FILAMENT_SENSOR_STATE", b"\x01", address=address)
            mask = self.box_state.get_Tn_inner_data(address, "filament_sensor", subpart="connections")
            self.gcode.respond_info("cmd_get_five_way_state= %d" % mask)
            # The legacy diagnostic also presents the cache after a failed
            # query. This assignment does not establish a fresh observation.
            self.box_state.Tn_data[name]["filament"] = next(
                (slot for index, slot in enumerate("ABCD") if mask & (1 << index)), "None"
            )

    def register_data_commands(self, gcode, webhooks):
        self.gcode = gcode
        for name, handler in (
            ("BOX_MODIFY_TN", self.cmd_modify_Tnn_map),
            ("BOX_MODIFY_TN_DATA", self.cmd_modify_Tn_data),
            ("BOX_MODIFY_TN_INNER_DATA", self.cmd_modify_Tn_inner_data),
            ("BOX_SHOW_TNN_INNER_DATA", self.cmd_show_Tnn_data),
            ("BOX_ENABLE_AUTO_REFILL", self.cmd_set_enable_auto_refill),
            ("BOX_ENABLE_CFS_PRINT", self.cmd_box_enable_CFS_print),
            ("BOX_UPDATE_SAME_MATERIAL_LIST", self.cmd_update_same_material_list),
            ("SAVE_BOX_CFG", self.cmd_SAVE_BOX_CFG),
            ("MODIFY_BOX_CFG", self.cmd_MODIFY_BOX_CFG),
        ):
            gcode.register_command(name, handler)
        webhooks.register_endpoint("box/get_box_cfg", self._handle_get_box_cfg_request)

    def get_status(self, eventime):
        return self.box_state.get_status(
            cut_pos=self.find_cut_pos,
            t_command=self.t_command,
            custom_command_result=self.custom_command_result,
        )

    def _handle_get_box_cfg_request(self, web_request):
        try:
            response = self.boxcfg.get_box_cfg()
        except Exception:
            # The stock endpoint discards a partial response on construction
            # errors, including configurations without optional positions.
            logging.exception("Could not construct box configuration response")
            response = {}
        web_request.send(response)
        return response

    @staticmethod
    def _data_command(gcmd, function, *args, **kwargs):
        try:
            return function(*args, **kwargs)
        except (OSError, ValueError) as error:
            raise gcmd.error("cfs: %s" % error) from error

    def cmd_modify_Tnn_map(self, gcmd):
        self._data_command(
            gcmd, self.box_state.modify_mapping, gcmd.get_command_parameters()
        )
        logging.info("Tnn_map: %s", self.box_state.Tnn_map)

    def cmd_modify_Tn_data(self, gcmd):
        address = gcmd.get_int("ADDR", minval=1, maxval=4)
        parameters = gcmd.get_command_parameters()
        self._data_command(
            gcmd, self.box_state.modify_Tn_data, address, parameters.get("DATA"),
            parameters.get("PART"), parameters.get("NUM"),
        )

    def cmd_modify_Tn_inner_data(self, gcmd):
        address = gcmd.get_int("ADDR", minval=1, maxval=4)
        parameters = gcmd.get_command_parameters()
        # The stock public command supplies no sensor subpart.
        self._data_command(
            gcmd, self.box_state.modify_Tn_inner_data, address, parameters.get("DATA"),
            parameters.get("PART"), parameters.get("NUM"),
        )

    def cmd_show_Tnn_data(self, gcmd):
        presented = deepcopy(self.box_state.Tn_data)
        for address in range(1, 5):
            colors = presented["T%d" % address]["color_value"]
            for slot, color in enumerate(colors):
                if isinstance(color, str) and color[1:7] in COLOR_NAMES:
                    colors[slot] = COLOR_NAMES[color[1:7]]
        self.gcode.respond_info("Tn_data: %s" % presented)

    def cmd_set_enable_auto_refill(self, gcmd):
        self.box_state.Tn_data["auto_refill"] = gcmd.get_int("ENABLE", 1)

    def cmd_box_enable_CFS_print(self, gcmd):
        enabled = gcmd.get_int("ENABLE", minval=0, maxval=1)
        self.box_state.modify_Tn_data(0, enabled, uppart="enable")
        # CFS uses its custom head event while ordinary runout is disabled.
        self.set_head_runout_enabled(1 - enabled)
        if not self._data_command(gcmd, self.store.update, "enable", enable=enabled):
            raise gcmd.error("cfs: could not save CFS-print enable")

    def cmd_update_same_material_list(self, gcmd):
        self.box_state.update_same_material_list(
            material_name=self.store.get_material_name
        )

    def cmd_MODIFY_BOX_CFG(self, gcmd):
        try:
            self.boxcfg.modify(gcmd.get_command_parameters())
        except (OSError, ValueError) as error:
            raise gcmd.error("MODIFY_BOX_CFG: failed, error '%s'" % error) from error
        description = "".join(
            "%s=%s,\n" % (_field_name(key), getattr(self.boxcfg, _field_name(key)))
            for key, value in self.boxcfg.modify_dict.items() if value
        )
        self.gcode.respond_info(
            "MODIFY_BOX_CFG: success, %s please use SAVE_BOX_CFG to save box.cfg"
            % description
        )

    def cmd_SAVE_BOX_CFG(self, gcmd):
        pending = dict(self.boxcfg.modify_dict)
        description = ",".join(
            "%s=%s" % (_field_name(key), value)
            for key, value in pending.items() if value
        )
        try:
            if self.config_paths is None:
                self.boxcfg.save()
            else:
                self.boxcfg.save(paths=self.config_paths)
        except (OSError, ValueError) as error:
            raise gcmd.error("SAVE_BOX_CFG error: %s" % error) from error
        self.gcode.respond_info(
            "SAVE_BOX_CFG ok: " + description if pending else "SAVE_BOX_CFG ok:no save"
        )


def load_config(config):
    """Reject activation before looking up or changing any printer objects."""
    raise config.error(
        "cfs: development scaffold; printer operation is not implemented."
    )
