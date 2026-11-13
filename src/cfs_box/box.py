"""Data-only box integration; the Klipper bootstrap still refuses activation.

Explicit construction supports PC checks of state, settings and storage. It
does not obtain a printer or a serial object, start timers, or run mechanics.
"""

from copy import deepcopy
import logging

from .configuration import BoxConfiguration, _field_name
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


class Box:
    """Compose implemented data behavior without activating an unfinished box.

    ``base_dir`` is supplied by the caller; PC tests always use a temporary
    directory. Only the data commands are registered explicitly, on substitutes
    until lifecycle and mechanical operations have been implemented.
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
