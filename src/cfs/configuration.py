"""Load the stock box settings and edit only their configuration section."""

import ast
from copy import deepcopy
import operator
import os
from pathlib import Path
import shutil
import tempfile


# Command order also determines which prefix wins in the legacy save format.
MODIFY_FIELDS = (
    "PRE_CUT_POS_X", "PRE_CUT_POS_Y", "CUT_POS_X", "CUT_POS_Y",
    "CUT_VELOCITY", "TN_RETRUDE", "TN_RETRUDE_VELOCITY", "BUFFER_EMPTY_LEN",
    "CLEAN_LEFT_POS_X", "CLEAN_LEFT_POS_Y", "CLEAN_RIGHT_POS_X",
    "CLEAN_RIGHT_POS_Y", "CLEAN_LEFT_POS_Z", "CLEAN_RIGHT_POS_Z",
    "CLEAN_VELOCITY", "EXTRUDE_POS_X", "EXTRUDE_POS_Y", "EXTRUDE_POS_Z",
    "TN_LINE_LEN", "CLEAN_RUN_COUNT", "CUT_RUN_COUNT",
)
CONFIG_PATHS = (
    Path("/usr/data/printer_data/config/box.cfg"),
    Path("/mnt/UDISK/printer_data/config/box.cfg"),
)
_OPERATORS = {
    ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
    ast.Div: operator.truediv, ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
}


def _field_name(command_key):
    lowered = command_key.lower()
    return "T" + lowered[1:] if command_key.startswith("T") else lowered


def _expression(value):
    """Accept literals and numeric arithmetic without executing Python code."""
    def evaluate(node):
        if isinstance(node, ast.Constant) and type(node.value) in (
            int, float, bool, str, type(None)
        ):
            return node.value
        if isinstance(node, (ast.List, ast.Tuple)):
            values = [evaluate(child) for child in node.elts]
            return values if isinstance(node, ast.List) else tuple(values)
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
            operand = evaluate(node.operand)
            if type(operand) in (int, float):
                return operand if isinstance(node.op, ast.UAdd) else -operand
        if isinstance(node, ast.BinOp) and type(node.op) in _OPERATORS:
            left, right = evaluate(node.left), evaluate(node.right)
            if type(left) in (int, float) and type(right) in (int, float):
                return _OPERATORS[type(node.op)](left, right)
        raise ValueError("only literals and numeric +, -, *, /, //, % are supported")

    try:
        return evaluate(ast.parse(value, mode="eval").body)
    except (SyntaxError, TypeError, ArithmeticError, RecursionError) as error:
        raise ValueError("invalid configuration expression: %s" % error) from error


class BoxConfiguration:
    def __init__(self, config):
        self.config = config
        self.modify_dict = {}
        self.bus = config.get("bus", None)
        if self.bus is None:
            raise config.error("the bus of box is not configured; set the serial_485 section name")
        self.version = config.getint("version", 0, minval=0, maxval=1)
        self.has_extrude_pos = int(config.getint("has_extrude_pos", 0, minval=0) == 1)
        self.filament_sensor = config.get("filament_sensor", None)
        self.switch_pin = config.get("switch_pin", None)
        axes, feedrate = self._limits(include_z=bool(self.has_extrude_pos))
        xmin, xmax = axes["x"]
        ymin, ymax = axes["y"]
        self.cut_pos_y_max = ymax - 0.1
        self.box_extrude_retry_num = config.getint("box_extrude_retry_num", 5, minval=0, maxval=5)
        self.box_first_clean_length = config.getint("box_first_clean_length", 90, minval=0, maxval=600)
        self.box_need_clean_length = config.getint(
            "box_need_clean_length", 80, minval=0, maxval=self.box_first_clean_length
        )
        self.box_need_clean_length_max = config.getint("box_need_clean_length_max", 100, minval=0, maxval=600)
        for name, default in (
            ("cut_run_count", 1), ("clean_run_count", 1), ("cr_machine", 1),
            ("extrude_material_times", 6),
        ):
            setattr(self, name, config.getint(name, default))
        for name, default, lower, upper in (
            ("pre_cut_pos_x", 0, xmin, xmax), ("pre_cut_pos_y", 0, ymin, ymax),
            ("cut_pos_x", -6.5, xmin, xmax), ("cut_pos_y", 0, ymin, ymax),
            ("cut_pos_offset", 0, -1, 5), ("cut_find_start_offset_y", 6, 0, 20),
            ("cut_velocity", 6000, 1200, feedrate), ("cut_accel", 5000, 1000, 10000),
            ("cut_accel_to_decel", 30000, 10000, 50000),
            ("Tn_retrude", -60, -120, -1), ("Tn_extrude_temp", 220, 180, 300),
            ("nozzle_volume", 200, 0, 300), ("flush_multiplier", 1, 0, 10),
            ("Tn_extrude", 140, 0, 200), ("buffer_empty_len", 30, 0, 60),
            ("diff_length", -10, -30, 30), ("extrude_material_len_for_box", 3, 0, 60),
            ("extrude_material_len_for_extruder", 18, 0, 60),
        ):
            setattr(self, name, config.getfloat(name, default, minval=lower, maxval=upper))
        for name, default in (("extrude_material_velocity", 180), ("Tn_line_len", 1)):
            setattr(self, name, config.getfloat(name, default))
        self.tn_final_extrude_len = config.getfloat("tn_final_extrude_len", -2.5)
        self.Tn_retrude_velocity = config.getintlist("Tn_retrude_velocity", (360,))
        self.Tn_extrude_percent = config.getintlist("Tn_extrude_percent", (1,))
        self.Tn_extrude_velocity = config.getintlist("Tn_extrude_velocity", (360,))
        if len(self.Tn_extrude_percent) != len(self.Tn_extrude_velocity):
            raise config.error("Tn_extrude_percent[%s] and Tn_extrude_velocity[%s] mismatch." % (
                len(self.Tn_extrude_percent), len(self.Tn_extrude_velocity)
            ))
        if self.has_extrude_pos:
            zmin, zmax = axes["z"]
            for name, default, lower, upper in (
                ("clean_left_pos_x", 150, xmin, xmax),
                ("clean_left_pos_y", 368, ymin, ymax),
                ("clean_left_pos_z", zmin + 25, zmin, zmax),
                ("clean_right_pos_x", 170, xmin, xmax),
                ("clean_right_pos_y", 368, ymin, ymax),
                ("clean_right_pos_z", zmin + 25, zmin, zmax),
                ("clean_velocity", 6000, 1200, feedrate),
                ("extrude_pos_x", 127, xmin, xmax),
                ("extrude_pos_y", 368, ymin, ymax),
                ("extrude_pos_z", zmin + 25, zmin, zmax),
                ("clean_pos_min_x", 152, xmin, xmax),
                ("clean_pos_min_y", 362, ymin, ymax),
                ("clean_pos_max_x", 156, xmin, xmax),
                ("clean_pos_max_y", 368, ymin, ymax),
                ("safe_pos_y", 350, ymin, ymax),
                ("retrude_position_x", 42, xmin, xmax),
                ("retrude_position_y", 100, ymin, ymax),
            ):
                setattr(self, name, config.getfloat(name, default, minval=lower, maxval=upper))

    def _limits(self, include_z=True):
        axes = {}
        for axis in ("xyz" if include_z else "xy"):
            section = self.config.getsection("stepper_" + axis)
            axes[axis] = (section.getfloat("position_min"), section.getfloat("position_max"))
        feedrate = self.config.getsection("printer").getfloat("max_velocity") * 60
        return axes, feedrate

    def get_real_cut_pos_y(self):
        return min(self.cut_pos_y + self.cut_pos_offset, self.cut_pos_y_max)

    def get_box_cfg(self):
        """Return the established endpoint shape; missing positions stay missing."""
        axes, feedrate = self._limits()
        bounds = {
            "cut_velocity": (1200, feedrate), "Tn_retrude": (-120, -1),
            "Tn_retrude_velocity": (0, 99999), "buffer_empty_len": (0, 30),
            "clean_velocity": (1200, feedrate), "cut_run_count": (1, 10),
            "Tn_line_len": (1, 1000), "clean_run_count": (1, 10),
        }
        result = {}
        for key in MODIFY_FIELDS:
            name = _field_name(key)
            value = getattr(self, name)
            if name == "cut_pos_y":
                value = self.get_real_cut_pos_y()
            elif name == "Tn_retrude_velocity":
                try:
                    value = value[0]
                except (IndexError, TypeError, KeyError):
                    pass
            lower, upper = bounds[name] if name in bounds else axes[name[-1]]
            result[name] = {"value": deepcopy(value), "min": lower, "max": upper}
        return result

    def modify(self, parameters):
        """Apply supported keys in command order, keeping earlier successful edits."""
        for key in MODIFY_FIELDS:
            raw = parameters.get(key)
            # Truthiness applies to the supplied expression, not its result:
            # the string "0" changes RAM even though SAVE skips the resulting 0.
            if raw:
                value = _expression(raw)
                self.modify_dict[key] = value
                setattr(self, _field_name(key), value)
        return deepcopy(self.modify_dict)

    def save(self, paths=CONFIG_PATHS, sync=None):
        """Back up and atomically save [box], preserving pending edits on failure.

        Retain legacy prefix matching, truthy-only saves and insertion at the
        next section. At EOF missing keys remain pending. Unlike the original
        global prefix replacement, edits cannot affect another section.
        """
        if not self.modify_dict:
            return None
        pending = deepcopy(self.modify_dict)
        # Values must stay on a single config line: a string literal may contain
        # newline escapes even though MODIFY never executes arbitrary Python.
        formatted = {key: self._save_value(value) for key, value in pending.items() if value}
        if sync is None:
            sync = getattr(os, "sync", lambda: None)
        paths = tuple(Path(path) for path in paths)
        if not paths:
            raise ValueError("at least one configuration path is required")
        path = next((candidate for candidate in paths if candidate.exists()), paths[-1])
        data = path.read_bytes()
        shutil.copy2(path, path.with_name(path.name + ".1"))
        sync()
        output = []
        in_box = False
        for line in data.decode("utf-8", errors="surrogateescape").splitlines(keepends=True):
            if line.startswith("[box]"):
                in_box = True
            elif in_box and line.startswith("["):
                if output and not output[-1].strip():
                    output.pop()
                for key, value in pending.items():
                    if value:
                        output.append("%s:%s\n" % (_field_name(key), formatted[key]))
                pending = {}
                in_box = False
            if in_box:
                for key, value in pending.items():
                    name = _field_name(key)
                    if value and line.startswith(name):
                        comment = "#" + line.split("#", 1)[1] if "#" in line else "\n"
                        line = "%s:%s%s" % (name, formatted[key], comment)
                        if not line.endswith("\n"):
                            line += "\n"
                        pending[key] = None
                        break
            output.append(line)
        encoded = "".join(output).encode("utf-8", errors="surrogateescape")
        descriptor, temporary = tempfile.mkstemp(prefix=".box-", dir=str(path.parent))
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            shutil.copystat(path, temporary)
            os.replace(temporary, path)
            sync()
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        self.modify_dict = pending
        return path

    @staticmethod
    def _save_value(value):
        if isinstance(value, (list, tuple)):
            text = ",".join(str(entry) for entry in value)
        else:
            text = str(value)
        if "\n" in text or "\r" in text:
            raise ValueError("configuration values cannot contain line breaks")
        return text
