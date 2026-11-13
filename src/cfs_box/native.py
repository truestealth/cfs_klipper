"""Checked requests through the borrowed stock serial_485 object.

The stock library owns framing, UART access and its two native retry layers.
This module only preserves the box sender's additional MCU CRC/LENGTH retries.
"""

from dataclasses import dataclass
import math


COMMANDS = {
    "CREATE_CONNECT": 0x01, "GET_RFID": 0x02, "GET_REMAIN_LEN": 0x03,
    "SET_BOX_MODE": 0x04, "GET_BUFFER_STATE": 0x05,
    "CTRL_MATERIAL_MOTOR_ACTION": 0x06, "CTRL_CONNECTION_MOTOR_ACTION": 0x07,
    "GET_FILAMENT_SENSOR_STATE": 0x08, "SET_MOTOR_SPEED": 0x09,
    "GET_BOX_STATE": 0x0A, "SET_PRE_LOADING": 0x0D, "MEASURING_WHEEL": 0x0E,
    "TIGHTEN_UP_ENABLE": 0x0F, "EXTRUDE_PROCESS": 0x10,
    "RETRUDE_PROCESS": 0x11, "EXTRUDE_PROCESS_MODEL2": 0x13,
    "GET_VERSION_SN": 0x14, "GET_HARDWARE_STATUS": 0x15,
    "COMMUNICATION_TEST": 0x55,
}
RESERVED_COMMANDS = frozenset((0x06, 0x09, 0x15))
_NO_DATA = frozenset((0x01, 0x05, 0x0A, 0x14))
_LENGTHS = {0x02: 1, 0x03: 1, 0x04: 2, 0x07: 1, 0x08: 1,
            0x0D: 2, 0x0E: 1, 0x0F: 1, 0x11: 2, 0x13: 2, 0x55: 1}
_SLOT_BITS = (1, 2, 4, 8)
_TIMEOUTS = {0x0A: 3600, 0x0F: 150, 0x10: 15, 0x11: 150, 0x13: 150, 0x55: 0.1}


class UnsupportedRequest(ValueError):
    """A recovered body is not an established executable operation."""


class FrameError(ValueError):
    """The returned bytes cannot be attributed to the requested exchange."""


@dataclass(frozen=True)
class Request:
    address: int
    command: int
    data: bytes
    timeout: float
    request_state: int = 0xFF
    diagnostic: bool = False

    @property
    def body(self):
        # Native serial appends F7 and CRC; neither belongs in this argument.
        return bytes((self.address, len(self.data) + 3, self.request_state,
                      self.command)) + self.data


@dataclass(frozen=True)
class Frame:
    address: int
    command: int
    status: int
    data: bytes
    raw: bytes


def _byte(value, name):
    if type(value) is not int or not 0 <= value <= 255:
        raise ValueError("%s must be an integer byte" % name)
    return value


def _data(value):
    if not isinstance(value, (bytes, bytearray, memoryview)):
        raise ValueError("request data must be bytes")
    value = bytes(value)
    if len(value) > 252:
        raise ValueError("request data exceeds the one-byte length field")
    return value


def _timeout(value):
    if type(value) not in (int, float) or value < 0 or not math.isfinite(value):
        raise ValueError("timeout must be finite, non-negative seconds")
    return value


def slot_mask(slot, allow_zero=False):
    """Encode the letter selectors used by mode/retract/staged-feed helpers."""
    if slot in ("A", "B", "C", "D"):
        return 1 << "ABCD".index(slot)
    if allow_zero and slot == "0":
        return 0
    raise ValueError("slot must be A/B/C/D%s" % (" or 0" if allow_zero else ""))


def build_request(address, command, data=b"", timeout=None, encoding=1):
    """Build a typed stock request, preserving its command-specific timeout.

    Host encoding 0 and opcode 13 can be represented for offline checks. Their
    known unresolved routes remain blocked by exchange(), independently of
    firmware identity. Stage values are wire bytes, not log-stage names.
    """
    _byte(address, "address")
    if address not in range(1, 5):
        raise ValueError("CFS address must be 1 through 4")
    if isinstance(command, str):
        if command not in COMMANDS:
            raise ValueError("unknown command name")
        command = COMMANDS[command]
    _byte(command, "command")
    if command not in COMMANDS.values():
        raise ValueError("unknown stock box command")
    if command in RESERVED_COMMANDS:
        raise UnsupportedRequest("reserved identifier has no recovered typed payload")
    payload = _data(data)
    length = 0 if command in _NO_DATA else _LENGTHS.get(command)
    if command == 0x10:
        if type(encoding) is not int or encoding not in (0, 1):
            raise ValueError("host encoding must be 0 or 1")
        length = 2 if encoding == 0 else 3
    if len(payload) != length:
        raise ValueError("wrong payload length for command 0x%02x" % command)
    if command in (0x02, 0x03, 0x0D) and payload[0] > 15:
        raise ValueError("slot mask must contain only four bits")
    if command in (0x04, 0x11) and payload[0] not in (0,) + _SLOT_BITS:
        raise ValueError("mode/retract selector must be one slot or zero")
    if command in (0x10, 0x13) and payload[0] not in _SLOT_BITS:
        raise ValueError("feed selector must be one physical slot")
    if command in (0x04, 0x11, 0x13) and payload[1] not in (0, 1):
        raise ValueError("invalid mode or trigger byte")
    if command == 0x10 and encoding == 0 and payload[1] not in (0, 1):
        raise ValueError("invalid legacy feed trigger")
    if command == 0x07 and payload[0] not in (0, 1, 2):
        raise ValueError("invalid connection-motor action")
    if command in (0x08, 0x0E, 0x0F) and payload[0] not in (0, 1):
        raise ValueError("invalid bank, wheel action or tighten-up enable")
    if command == 0x0D and payload[1] not in (0, 1, 2, 3):
        raise ValueError("invalid preload action")
    default = _TIMEOUTS.get(command, 2)
    if command == 0x0D and payload[1] in (2, 3):
        default = 300 * sum(bool(payload[0] & bit) for bit in _SLOT_BITS)
    return Request(address, command, payload, _timeout(default if timeout is None else timeout))


def diagnostic_request(address, request_state, command, data=b"", timeout=5):
    """Represent the public raw-body diagnostic API, without a typed schema."""
    _byte(address, "address")
    _byte(request_state, "request state")
    _byte(command, "command")
    if command > 32:
        raise ValueError("diagnostic CMD must be 0 through 32")
    _timeout(timeout)
    if type(timeout) is not int or not 1 <= timeout <= 120:
        raise ValueError("diagnostic timeout must be integer 1 through 120 seconds")
    return Request(address, command, _data(data), timeout, request_state, True)


def crc8(data):
    value = 0
    for byte in data:
        value ^= byte
        for _ in range(8):
            value = ((value << 1) ^ (0x07 if value & 0x80 else 0)) & 0xFF
    return value


def validate_reply(raw, address, command):
    if not isinstance(raw, (bytes, bytearray, memoryview)):
        raise FrameError("native reply is not framed bytes")
    raw = bytes(raw)
    if len(raw) < 6 or raw[0] != 0xF7:
        raise FrameError("invalid reply header or minimum length")
    if raw[2] < 3 or len(raw) != raw[2] + 3:
        raise FrameError("reply length does not match LEN")
    if crc8(raw[2:-1]) != raw[-1]:
        raise FrameError("reply CRC mismatch")
    if raw[1] != address or raw[4] != command:
        raise FrameError("reply address/command does not match the request")
    return Frame(raw[1], raw[4], raw[3], raw[5:-1], raw)


class NativeSerial:
    """Borrow an existing native object without acquiring its connection."""

    def __init__(self, native):
        if not callable(getattr(native, "cmd_send_data_with_response", None)):
            raise ValueError("stock native request method is unavailable")
        self.native = native

    @classmethod
    def from_printer(cls, printer, bus):
        if not isinstance(bus, str) or not bus:
            raise ValueError("a configured serial_485 bus name is required")
        return cls(printer.lookup_object("serial_485 " + bus))

    @property
    def is_connected(self):
        return self.native.is_connected

    def register_response(self, callback, address, command):
        """Delegate a filter; registration does not bypass receive admission."""
        _byte(address, "address")
        _byte(command, "command")
        address_tag = "" if command == 0xA0 else "#addr_%d" % address
        return self.native.register_response(callback, address_tag, "#cmd_%d" % command)

    def exchange(self, request, retries_en=None, check_context=None):
        """Return a checked frame/None, with only the recovered sender retries.

        The ordinary call has two arguments. Explicit False remains a third
        argument, but cannot turn off the retained library's own retry layers.
        A caller-supplied context guard raises if the address identity changed;
        it runs before every API attempt, including CRC/LENGTH resends.
        """
        if not isinstance(request, Request):
            raise ValueError("exchange requires a built Request")
        if retries_en is not None and type(retries_en) is not bool:
            raise ValueError("retries_en must be None, False or True")
        if check_context is not None and not callable(check_context):
            raise ValueError("check_context must be callable or None")
        if request.command == 0x13:
            raise UnsupportedRequest("legacy opcode 13 has an unresolved host/MCU payload mismatch")
        if not request.diagnostic and request.command == 0x10:
            if len(request.data) != 3 or request.data[1] > 7:
                raise UnsupportedRequest("only the established encoding-1 wire stages 0 through 7 may execute")
        attempts = 3 if request.timeout >= 3600 else 7
        for _ in range(attempts):
            arguments = (request.body, request.timeout)
            if retries_en is not None:
                arguments += (retries_en,)
            if check_context is not None:
                check_context()
            raw = self.native.cmd_send_data_with_response(*arguments)
            if raw is None or (request.command == 0x55 and not raw):
                return None
            frame = validate_reply(raw, request.address, request.command)
            if request.command == 0x55 or frame.status not in (0x02, 0x04):
                return frame
        return frame
