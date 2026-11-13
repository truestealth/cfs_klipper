"""Data and borrowed-native integration; the bootstrap still refuses activation.

Explicit construction supports PC checks of state, settings and storage. It
does not obtain a printer or a serial object, start timers, or run mechanics.
"""

from copy import deepcopy
import logging

from .configuration import BoxConfiguration, _field_name
from .firmware import DeviceVersions
from .native import NativeSerial, Request, UnsupportedRequest, build_request, diagnostic_request, slot_mask
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
    directory. Data and native command groups are registered explicitly on
    substitutes until lifecycle and mechanical operations have been implemented.
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
        self.move_z = 0.0
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
        self.error_tnn = {}
        self.box_filament_err = False
        self.is_use_ending_material = False
        self.inside_extrusion_all_materials = False
        self.print_state = ""
        self.filament_error_message = None

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
        self._binding_generation += 1
        self.native, self._reactor = adapter, reactor
        self.device_versions = versions
        self._effect_handlers = handlers
        self.last_replies.clear()
        self.state_extensions.clear()
        for address in range(1, 5):
            self.box_state.modify_Tn_data(address, "-1", "version")
            self.box_state.modify_Tn_data(address, "-1", "sn")

    def unbind_native(self):
        """Invalidate own pending work without closing the borrowed transport."""
        self._binding_generation += 1
        for address in range(1, 5):
            self.invalidate_device(address)
        self.native = None
        self._reactor = None
        self._effect_handlers.clear()
        self.last_replies.clear()
        self.state_extensions.clear()

    def invalidate_device(self, address, raw_reply=None):
        self.device_versions.invalidate(address, raw_reply=raw_reply)
        self.state_extensions.pop(address, None)
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

    def send_native(self, request, context=None, retries_en=None):
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
            context = DecodeContext(
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
            handlers[effect.name](*effect.args)
            if effect.name == "identity" and binding == self._binding_generation:
                # Accepting this request's own new identity legitimately
                # advances its generation; external changes never do.
                generation = self.device_versions.generation(address)
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
            raise gcmd.error("cfs_box: %s" % error) from error

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
            raise gcmd.error("cfs_box: %s" % error) from error

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
            raise gcmd.error("cfs_box: %s" % error) from error

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
        self.gcode.run_script_from_command(
            "SET_FILAMENT_SENSOR SENSOR=filament_sensor_2 ENABLE=%d" % (1 - enabled)
        )
        if not self._data_command(gcmd, self.store.update, "enable", enable=enabled):
            raise gcmd.error("cfs_box: could not save CFS-print enable")

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
        "cfs_box: development scaffold; printer operation is not implemented."
    )
