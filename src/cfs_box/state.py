"""Host state and metadata; observations never imply completed CFS motion."""

from copy import deepcopy
import logging

from .storage import CACHE_FIELDS, TOOLS, new_base_data


BOX_FIELDS = (
    "state", "filament", "temperature", "dry_and_humidity",
    "filament_detected", "measuring_wheel", "version", "sn", "mode",
    "vender", "remain_len", "color_value", "material_type", "uuid",
)
UPPER_FIELDS = ("filament", "state", "auto_refill", "enable", "filament_useup", "same_material")
SLOTS = "ABCD"


class BoxState:
    """Keep live state shared with retained consumers and snapshots isolated.

    Saved labels are loaded into a separate cache. They enter live state only
    when a caller supplies a slot-presence observation to sync_tn_data().
    """

    def __init__(self, store=None):
        self.store = store
        self.Tn_data = {
            "filament": 1, "state": "None", "auto_refill": 1,
            "enable": 1, "filament_useup": 1, "same_material": [],
        }
        self.Tn_inner_data = {"buffer": None}
        for address in range(1, 5):
            name = "T%d" % address
            self.Tn_data[name] = {
                field: (["-1"] * 4 if field in CACHE_FIELDS
                        else "-1" if field in ("version", "sn") else "None")
                for field in BOX_FIELDS
            }
            self.Tn_inner_data[name] = {"filament_sensor": {"material": 0, "connections": 0}}
        self.Tnn_content = {
            tool: {"addr": int(tool[1]), "num": tool[2]} for tool in TOOLS
        }
        self.Tn_map = {"T%d" % index: tool for index, tool in enumerate(TOOLS)}
        self.Tnn_map = {tool: tool for tool in TOOLS}
        self.tn_save_data_path = store.tn_save_data_path if store else None
        self.tn_save_data = store.load() if store else new_base_data()
        self.extrude_err = False
        self.is_continue_print = False

    def get_status(self, cut_pos=0.0, t_command="", custom_command_result=""):
        result = deepcopy(self.Tn_data)
        result.update(cut_pos=cut_pos, t_command=t_command,
                      custom_command_result=custom_command_result)
        return result

    def get_Tnn_content(self, tnn):
        return self.Tnn_content[tnn]

    def get_Tnn_map(self):
        return self.Tnn_map

    def resolve_tool(self, name):
        """Resolve a virtual or physical logical name through the mutable map."""
        logical = self.Tn_map.get(name, name)
        if logical not in self.Tnn_map:
            raise ValueError("Unknown tool name: %s" % name)
        return self.Tnn_map[logical]

    def modify_mapping(self, parameters):
        for logical in self.Tnn_map:
            target = parameters.get(logical)
            if target in self.Tnn_map:
                self.Tnn_map[logical] = target
        if self.store and not self.store.save_mapping(self.Tnn_map):
            raise OSError("Unable to save CFS tool mapping")

    def get_Tn_data(self, addr, part=None, num=None, uppart=None):
        if uppart in UPPER_FIELDS:
            return self.Tn_data[uppart]
        if not 1 <= addr <= 4 or part not in BOX_FIELDS:
            logging.warning("Invalid CFS address or state field: %s, %s", addr, part)
            return None
        value = self.Tn_data["T%d" % addr][part]
        if part in CACHE_FIELDS:
            if num not in ("A", "B", "C", "D"):
                logging.warning("CFS slot field requires NUM=A/B/C/D")
                return None
            return value[SLOTS.index(num)]
        return value

    def modify_Tn_data(self, addr, data, part=None, num=None, uppart=None):
        if uppart in UPPER_FIELDS:
            self.Tn_data[uppart] = data
            return
        if not 1 <= addr <= 4 or part not in BOX_FIELDS:
            logging.warning("Invalid CFS address or state field: %s, %s", addr, part)
            return
        target = self.Tn_data["T%d" % addr]
        if part not in CACHE_FIELDS:
            target[part] = data
            return
        if not isinstance(num, str) or num not in ("A", "B", "C", "D"):
            logging.warning("CFS slot field requires NUM=A/B/C/D")
            return
        index = SLOTS.index(num)
        target[part][index] = data
        # Empty live identity keeps its cached label for future observations.
        if data != "none":
            if self.store:
                saved = self.store.update_metadata(addr, part, num, data)
                self.tn_save_data = self.store.base_data
                if not saved:
                    raise OSError("Unable to save CFS slot metadata")
            else:
                self.tn_save_data["T%d" % addr][part][index] = deepcopy(data)

    def get_Tn_inner_data(self, addr, part=None, num=None, subpart=None, uppart=None):
        if uppart == "buffer":
            return self.Tn_inner_data["buffer"]
        if not 1 <= addr <= 4 or part != "filament_sensor" or subpart not in ("material", "connections"):
            logging.warning("Invalid CFS inner state field or sensor subpart")
            return None
        return self.Tn_inner_data["T%d" % addr][part][subpart]

    def modify_Tn_inner_data(self, addr, data, part=None, num=None, subpart=None, uppart=None):
        if uppart == "buffer":
            self.Tn_inner_data["buffer"] = data
            return
        if not 1 <= addr <= 4 or part != "filament_sensor" or subpart not in ("material", "connections"):
            logging.warning("Invalid CFS inner state field or sensor subpart")
            return
        self.Tn_inner_data["T%d" % addr][part][subpart] = data

    def get_box_mode(self, addr):
        return self.get_Tn_data(addr, "mode")

    def clear_e_err(self):
        self.extrude_err = False

    def e_err_set(self):
        self.extrude_err = True

    def sync_tn_data(self, tn, material_status):
        """Apply cached labels only after an explicit slot-presence mask.

        A present slot with no cached vendor remains unknown. An absent slot
        changes only its live vendor; saved metadata and other labels survive.
        """
        live, saved = self.Tn_data[tn], self.tn_save_data[tn]
        for index in range(4):
            if not material_status & (1 << index):
                live["vender"][index] = "none"
            elif saved["vender"][index] != "-1":
                for field in CACHE_FIELDS:
                    live[field][index] = deepcopy(saved[field][index])
            else:
                live["vender"][index] = "unknown"
                if saved["color_value"][index] != "-1":
                    for field in ("color_value", "material_type"):
                        live[field][index] = deepcopy(saved[field][index])

    def update_same_material_list(self, material_name=None):
        groups = self.Tn_data["same_material"] = []
        for tool, content in self.Tnn_content.items():
            box = self.Tn_data["T%d" % content["addr"]]
            index = SLOTS.index(content["num"])
            vendor, kind, color = (box[field][index] for field in ("vender", "material_type", "color_value"))
            if vendor in ("none", "-1") or kind in ("-1", "none", "unknown") or color in ("-1", "none", "unknown"):
                continue
            for group in groups:
                if group[0] == kind and group[1] == color:
                    if tool not in group[2]:
                        group[2].append(tool)
                    break
            else:
                name = ""
                if material_name:
                    try:
                        name = material_name(kind)
                    except (OSError, ValueError, TypeError, KeyError):
                        logging.exception("Unable to read CFS material name")
                groups.append([kind, color, [tool], name])
