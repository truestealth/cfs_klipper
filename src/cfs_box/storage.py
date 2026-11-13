"""Compatible spool metadata storage, independent of printer and serial objects."""

from copy import deepcopy
import json
import logging
import os
from pathlib import Path
import stat
import tempfile


CACHE_FIELDS = ("vender", "remain_len", "color_value", "material_type")
TOOLS = tuple("T%d%s" % (address, slot) for address in range(1, 5) for slot in "ABCD")
RECOVERY_FIELDS = ("enable", "tnn_map", "last_cmd")


def new_base_data():
    return {
        "T%d" % address: {field: ["-1"] * 4 for field in CACHE_FIELDS}
        for address in range(1, 5)
    }


def _validate_base_data(data):
    if not isinstance(data, dict):
        raise ValueError("base_data must be an object")
    for address in range(1, 5):
        box = data.get("T%d" % address)
        if not isinstance(box, dict):
            raise ValueError("base_data must contain T1 through T4")
        for field in CACHE_FIELDS:
            if not isinstance(box.get(field), list) or len(box[field]) != 4:
                raise ValueError("base_data.%s must contain four slot values" % field)


def _validate_mapping(mapping):
    if not isinstance(mapping, dict) or set(mapping) != set(TOOLS):
        raise ValueError("tnn_map must contain all sixteen physical tool names")
    if any(value not in TOOLS for value in mapping.values()):
        raise ValueError("tnn_map values must be physical tool names")


class SpoolStore:
    """Read established JSON explicitly and preserve separately owned files.

    A malformed existing spool file raises instead of being replaced by defaults.
    Writes use a temporary sibling and atomic replacement; a failed write returns
    False without claiming the cache was saved. Construction performs no I/O.
    """

    def __init__(self, base_dir):
        self.path = Path(base_dir) / "creality/userdata/box/tn_data.json"
        self.tn_save_data_path = str(self.path)
        self.material_database = self.path.with_name("material_database.json")
        self.base_data = None

    def _read_document(self):
        try:
            with self.path.open("r", encoding="utf-8") as stream:
                document = json.load(stream)
        except FileNotFoundError:
            return {}
        if not isinstance(document, dict):
            raise ValueError("tn_data.json must contain an object")
        if "base_data" in document:
            _validate_base_data(document["base_data"])
        return document

    def _write_document(self, document):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        mode = stat.S_IMODE(self.path.stat().st_mode) if self.path.exists() else None
        descriptor, temporary = tempfile.mkstemp(prefix="tn_data.", suffix=".tmp", dir=str(self.path.parent))
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump(document, stream, indent=4, ensure_ascii=False, allow_nan=False)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            if mode is not None:
                os.chmod(temporary, mode)
            os.replace(temporary, self.path)
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass

    def load(self):
        """Load the cache, creating it only when the file or base_data is absent."""
        document = self._read_document()
        if "base_data" not in document:
            document["base_data"] = new_base_data()
            self._write_document(document)
        loaded = deepcopy(document["base_data"])
        if self.base_data is None:
            self.base_data = loaded
        else:
            # State consumers retain this object across explicit reloads.
            self.base_data.clear()
            self.base_data.update(loaded)
        return self.base_data

    def _require_loaded(self):
        if self.base_data is None:
            raise RuntimeError("Load spool metadata before using its cache")

    @staticmethod
    def _slot(addr, part, num):
        if type(addr) is not int or addr not in range(1, 5):
            raise ValueError("CFS address must be 1 through 4")
        if part not in CACHE_FIELDS:
            raise ValueError("Unknown saved slot field: %s" % part)
        if num not in ("A", "B", "C", "D"):
            raise ValueError("Slot must be A, B, C or D")
        return "T%d" % addr, "ABCD".index(num)

    def get_slot(self, addr, part, num):
        self._require_loaded()
        box, index = self._slot(addr, part, num)
        return self.base_data[box][part][index]

    def _save(self, changes, remove=(), slot_change=None):
        try:
            document = self._read_document()
            if "base_data" not in document:
                self._require_loaded()
                document["base_data"] = deepcopy(self.base_data)
            for key in remove:
                document.pop(key, None)
            document.update(deepcopy(changes))
            if slot_change is not None:
                box, part, index, value = slot_change
                document["base_data"][box][part][index] = value
            self._write_document(document)
        except (OSError, TypeError) as error:
            logging.error("Could not save CFS spool metadata: %s", error)
            return False
        if slot_change is not None:
            # Keep the shared cache object while retaining data read after load.
            self.base_data.clear()
            self.base_data.update(deepcopy(document["base_data"]))
        return True

    def update_metadata(self, addr, part, num, data):
        """Save labels separately from live empty-slot identity observations."""
        self._require_loaded()
        box, index = self._slot(addr, part, num)
        if data == "none":
            return True
        return self._save({}, slot_change=(box, part, index, data))

    def update(self, part, enable=None, tnn_map=None, last_cmd=None):
        self._require_loaded()
        if part == "base_data":
            _validate_base_data(self.base_data)
            value = self.base_data
        elif part == "enable":
            if type(enable) is not int or enable not in (0, 1):
                raise ValueError("Saved enable must be integer 0 or 1")
            value = enable
        elif part == "tnn_map":
            _validate_mapping(tnn_map)
            value = tnn_map
        elif part == "last_cmd":
            if last_cmd not in TOOLS:
                raise ValueError("Saved source must be a physical tool name")
            value = last_cmd
        else:
            raise ValueError("Unknown saved data part: %s" % part)
        return self._save({part: value})

    def save_mapping(self, mapping):
        return self.update("tnn_map", tnn_map=mapping)

    def recovery_hints(self):
        """Read hints without applying them to live sensors or active operations."""
        document = self._read_document()
        hints = {key: deepcopy(document[key]) for key in RECOVERY_FIELDS if key in document}
        if "tnn_map" in hints:
            _validate_mapping(hints["tnn_map"])
        if "enable" in hints and (type(hints["enable"]) is not int or hints["enable"] not in (0, 1)):
            raise ValueError("Saved enable must be integer 0 or 1")
        if "last_cmd" in hints and hints["last_cmd"] not in TOOLS:
            raise ValueError("Saved source must be a physical tool name")
        return hints

    def cleanup_recovery(self):
        return self._save({}, remove=RECOVERY_FIELDS)

    def load_material_database(self):
        """Return catalogue records, or the established unavailable-data fallback."""
        try:
            with self.material_database.open("r", encoding="utf-8") as stream:
                document = json.load(stream)
            records = document.get("result", {}).get("list", [])
            if isinstance(records, list):
                return records
        except (OSError, ValueError, AttributeError):
            pass
        return []

    def _material_parameter(self, material_type, key):
        if not isinstance(material_type, str):
            return None
        for record in self.load_material_database():
            if not isinstance(record, dict):
                continue
            base, parameters = record.get("base"), record.get("kvParam")
            if not isinstance(base, dict) or not isinstance(parameters, dict):
                continue
            if base.get("id") == material_type[1:6] and parameters.get(key) is not None:
                return parameters[key]
        return None

    def get_material_name(self, material_type):
        if not isinstance(material_type, str):
            return ""
        for record in self.load_material_database():
            if not isinstance(record, dict):
                continue
            base = record.get("base")
            if not isinstance(base, dict):
                continue
            if base.get("id") == material_type[1:6] and base.get("meterialType") is not None:
                return base["meterialType"]
        return ""

    def get_material_temperature(self, material_type, fallback):
        try:
            value = self._material_parameter(material_type, "nozzle_temperature")
            return int(value) if value is not None else fallback
        except (ValueError, TypeError, OverflowError):
            return fallback

    def get_material_max_speed(self, material_type):
        try:
            value = self._material_parameter(material_type, "filament_max_volumetric_speed")
            return float(value) if value is not None else None
        except (ValueError, TypeError, OverflowError):
            return None
