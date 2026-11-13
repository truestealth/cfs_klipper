"""Command-specific reply data and the stock sender's contextual outcomes.

Frames have already passed native framing/address/command validation. Deferred
effects are explicit: the integration must bind them before claiming the reply
has been handled. A true sender outcome does not establish completed motion.
"""

from dataclasses import dataclass
import struct

from .firmware import parse_identity


STATUS_NAMES = {
    0x00: "OK", 0x01: "PARAMS_ERR", 0x02: "CRC_ERR", 0x03: "STATE_ERR",
    0x04: "LENGTH_ERR", 0x05: "EXTRUDE_ERR1", 0x08: "EXTRUDE_ERR4",
    0x09: "EXTRUDE_ERR5", 0x0A: "EXTRUDE_ERR6", 0x0B: "EXTRUDE_ERR7",
    0x0C: "EXTRUDE_ERR8", 0x0E: "EXTRUDE_ERR9", 0x0D: "EXTRUDE_ERR10",
    0x13: "RETRUDE_ERR1", 0x14: "RETRUDE_ERR2", 0x15: "RETRUDE_ERR3",
    0x16: "RETRUDE_ERR4", 0x17: "RETRUDE_ERR5", 0x19: "RETRUDE_ERR6",
    0x1A: "RETRUDE_ERR7", 0x22: "MOTOR_LOAD_ERR", 0x30: "UPDATE_STATE",
    0x50: "FILAMENT_ERR", 0x51: "SPEED_ERR", 0x52: "ENWIND_ERR",
}
_SLOTS = "ABCD"
_SLOT_NAMES = {0: "0", 1: "A", 2: "B", 4: "C", 8: "D", 15: "0"}


@dataclass(frozen=True)
class Effect:
    name: str
    args: tuple = ()


@dataclass(frozen=True)
class DecodeResult:
    value: object
    effects: tuple = ()


@dataclass
class DecodeContext:
    report_err: object = True
    last_cmd: object = None
    error: object = None
    last_err: object = None
    print_state: str = ""
    box_filament_err: object = False
    is_use_ending_material: object = False
    # The stage-4 formatter supplies the exact expected text for the original
    # duplicate-message comparison. Do not guess it from a key or truthiness.
    filament_error_message: object = None


def _length(data, expected, command):
    if len(data) != expected:
        raise ValueError("command 0x%02x needs %d reply data bytes, got %d" % (
            command, expected, len(data)))


def _request_byte(request):
    if not request.data:
        raise ValueError("command 0x%02x requires request data" % request.command)
    return request.data[0]


def _facts(frame, request, context, slot=None):
    return {
        "address": frame.address,
        "request_address": request.address,
        "command": request.command,
        "status": STATUS_NAMES.get(frame.status),
        "slot": slot,
        "source": context.last_cmd,
        "data_send": request.body,
    }


def _report(frame, request, context, key, slot=None):
    return Effect("report_error", (key, _facts(frame, request, context, slot)))


def _slot_effect(address, slot, part, value):
    return Effect("set_slot", (address, slot, part, value))


def _rfid_records(data):
    """Parse complete slot:value; records before applying any observations."""
    if not data:
        return {}
    if not data.endswith(b";"):
        raise ValueError("RFID records must end with a semicolon")
    records = {}
    for record in data.split(b";")[:-1]:
        if b":" not in record:
            raise ValueError("RFID record has no slot separator")
        slot, value = record.split(b":", 1)
        if len(slot) == 1 and chr(slot[0]) in _SLOTS:
            # The original joins chr(byte), rather than UTF-8 decoding.
            records[chr(slot[0])] = value.decode("latin-1")
    return records


def _rfid_effects(state, frame, request, context):
    records = _rfid_records(frame.data)
    effects = []
    saved_box = state.tn_save_data["T%d" % frame.address]
    live_box = state.Tn_data["T%d" % frame.address]
    for slot, value in records.items():
        index = _SLOTS.index(slot)
        if value not in ("none", "unknown") and len(value) != 40:
            facts = _facts(frame, request, context, slot)
            facts["rfid"] = value
            effects.append(Effect("report_error", ("key_rfid_err", facts)))
            if saved_box["vender"][index] == "unknown":
                color = saved_box["color_value"][index]
                kind = saved_box["material_type"][index]
                if color not in ("unknown", "-1"):
                    values = ("unknown", color, kind)
                else:
                    values = ("unknown", "unknown", "unknown")
            else:
                values = ("unknown", "unknown", "unknown")
            for part, item in zip(("vender", "color_value", "material_type"), values):
                effects.append(_slot_effect(frame.address, slot, part, item))
            continue

        effects.append(_slot_effect(frame.address, slot, "vender", value))
        if value == "none":
            values = ("none", "none")
        elif value == "unknown":
            if live_box["color_value"][index] != "-1":
                continue
            color = saved_box["color_value"][index]
            values = ((color, saved_box["material_type"][index])
                      if color not in ("unknown", "-1") else ("unknown", "unknown"))
        else:
            values = (value[17:24], value[11:17])
        for part, item in zip(("color_value", "material_type"), values):
            effects.append(_slot_effect(frame.address, slot, part, item))
    effects.append(Effect("schedule_groups"))
    return effects


def _normal_data(state, frame, request, profile, context):
    command, address, data = request.command, frame.address, frame.data
    if command == 0x0A:
        if profile is None:
            raise ValueError("a supported per-device firmware profile is required for state data")
        _length(data, profile.state_data_length, command)
        for part, index in (("temperature", 0), ("dry_and_humidity", 1), ("mode", 3)):
            state.modify_Tn_data(address, str(data[index]), part)
        return ([Effect("state_extension", (address, data[4], data[5]))]
                if len(data) == 6 else [])
    if command == 0x05:
        _length(data, 1, command)
        state.modify_Tn_inner_data(0, data[0], uppart="buffer")
    elif command == 0x08:
        _length(data, 1, command)
        bank = _request_byte(request)
        if bank not in (0, 1):
            raise ValueError("unknown filament sensor bank")
        state.modify_Tn_inner_data(address, data[0], "filament_sensor",
                                   subpart=("material", "connections")[bank])
    elif command == 0x03:
        _length(data, 4, command)
        mask = _request_byte(request)
        for index, slot in enumerate(_SLOTS):
            if mask & (1 << index):
                state.modify_Tn_data(address, str(data[index]), "remain_len", slot)
    elif command == 0x0E:
        action = _request_byte(request)
        if action == 1:
            _length(data, 4, command)
            value = struct.unpack(">f", data)[0]
        else:
            value = 0
        state.modify_Tn_data(address, value, "measuring_wheel")
    elif command == 0x14:
        parse_identity(data, raw_reply=frame.raw)
        return [Effect("identity", (address, data, frame.raw))]
    elif command == 0x02:
        return _rfid_effects(state, frame, request, context)
    return []


def decode_reply(state, frame, request, *, profile=None, context=None):
    """Decode one validated exchange and retain exact command/context outcomes.

    Normal observations are updated only after their whole payload validates.
    Side effects involving errors, scheduling or recovery are ordered records;
    the integration must preflight their handlers and then execute that order.
    """
    context = context if context is not None else DecodeContext()
    if frame is None:
        if request.command in (0x0A, 0x55):
            return DecodeResult(None)
        return DecodeResult(False, (Effect("timeout_process", (request.address,)),))
    if frame.address != request.address or frame.command != request.command:
        raise ValueError("reply attribution does not match the request")
    if request.command == 0x55:
        _length(frame.data, 1, request.command)
        return DecodeResult(frame.data[0])

    status, command = frame.status, request.command
    effects = []
    if command == 0x10:
        effects.append(Effect("snapshot_extrude_status", (STATUS_NAMES.get(status),)))
    if status == 0:
        effects.extend(_normal_data(state, frame, request, profile, context))
        return DecodeResult(True, tuple(effects))
    if status == 0x30:
        _length(frame.data, 4, command)
        effects.append(Effect("state_events", (frame.address, frame.data)))
        return DecodeResult(True, tuple(effects))
    if command == 0x0A and status == 0x50:
        if profile is None:
            raise ValueError("a supported per-device firmware profile is required for fault state data")
        _length(frame.data, profile.state_data_length, command)
        if len(frame.data) == 6:
            effects.append(Effect("state_extension", (frame.address, frame.data[4], frame.data[5])))

    if status == 0x01:
        effects.append(_report(frame, request, context, "key_params_err"))
    elif status == 0x03:
        if command == 0x10 and context.report_err:
            effects.append(Effect("warning", ("EXTRUDE_PROCESS returned STATE_ERR",)))
            return DecodeResult(True, tuple(effects))
        effects.extend((
            _report(frame, request, context, "key_state_err"),
            Effect("record_error", ("state_err", {"addr": frame.address})),
            Effect("error_address", (frame.address,)),
        ))
    elif status in (0x05, 0x09, 0x0B, 0x14, 0x15, 0x19, 0x1A):
        keys = {0x05: "key_extrude_err1", 0x09: "key_extrude_err4",
                0x0B: "key_joint_err", 0x14: "key_retrude_err1",
                0x15: "key_retrude_err2", 0x19: "key_retrude_err1",
                0x1A: "key_retrude_err1"}
        if command != 0x0A and context.report_err:
            effects.extend((_report(frame, request, context, keys[status]),
                            Effect("error_address", (frame.address,))))
    elif status == 0x08:
        if command != 0x0A:
            slot = _SLOT_NAMES[_request_byte(request)]
            effects.extend((_report(frame, request, context, "key_filament_err", slot),
                            Effect("set_global", ("filament", 0)),
                            Effect("error_address", (frame.address,))))
    elif status == 0x13:
        if context.report_err:
            effects.append(Effect("warning", ("Retraction reached the buffer-empty limit",)))
    elif status in (0x16, 0x17):
        if not context.last_cmd:
            effects.append(Effect("warning", ("Retraction has no recorded filament source",)))
            return DecodeResult(True, tuple(effects))
        effects.extend((_report(frame, request, context, "key_filament_err", context.last_cmd[2]),
                        Effect("set_global", ("filament", 0)),
                        Effect("error_address", (frame.address,))))
    elif status == 0x22:
        if context.report_err:
            effects.append(_report(frame, request, context, "key_load_err"))
    elif status == 0x50:
        if state.Tn_data["filament_useup"] == 0:
            effects.append(Effect("set_global", ("filament_useup", 1)))
        if not context.last_cmd:
            effects.append(Effect("warning", ("FILAMENT_ERR has no recorded filament source",)))
            return DecodeResult(True, tuple(effects))
        if context.box_filament_err is False:
            effects.extend((Effect("set_context", ("box_filament_err", True)),
                            Effect("schedule_tighten_up")))
        record = Effect("record_error", ("filament_err", {
            "last_tnn": context.last_cmd, "tnn": context.last_cmd, "addr": frame.address,
        }))
        if state.Tn_data["auto_refill"] and context.print_state in ("printing", "paused"):
            if context.is_use_ending_material is False:
                effects.extend((Effect("set_context", ("is_use_ending_material", True)), record))
            return DecodeResult(True, tuple(effects))
        if context.last_err is not None:
            if context.filament_error_message is None:
                raise ValueError("formatted filament error is required for duplicate comparison")
            if context.last_err == context.filament_error_message:
                return DecodeResult(False, tuple(effects))
        effects.extend((_report(frame, request, context, "key_filament_err", context.last_cmd[2]),
                        Effect("set_global", ("filament", 0)), record,
                        Effect("set_context", ("inside_extrusion_all_materials", True)),
                        Effect("run_gcode", ("BOX_EXTRUSION_ALL_MATERIALS",)),
                        Effect("set_context", ("inside_extrusion_all_materials", False))))
    elif status in (0x51, 0x52):
        if context.error != "filament_err" and context.last_cmd is not None:
            record = Effect("record_error", ("empty_print", {"tnn": context.last_cmd}))
            key = "key_speed_err" if status == 0x51 else "key_enwind_err"
            if status == 0x52:
                if state.Tn_data["filament_useup"] == 0:
                    effects.append(Effect("set_global", ("filament_useup", 1)))
                effects.extend((record, _report(frame, request, context, key)))
            else:
                effects.extend((_report(frame, request, context, key), record))
    # Exhausted MCU CRC/LENGTH, the remaining typed errors, and unknown status
    # all return False. Only the native adapter owns the additional retry loop.
    return DecodeResult(False, tuple(effects))
