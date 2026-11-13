"""Per-address CFS identity and the explicitly examined firmware differences."""

from dataclasses import dataclass
from typing import Optional


BASELINE_VERSION = "1.1.3"
SLOT_EVENT_DATA_LENGTH = 4
_BUFFERS = (bytes, bytearray, memoryview)


@dataclass(frozen=True)
class FirmwareProfile:
    version: str
    state_data_length: int
    rfid_lifecycle: bool
    rfid_tag_filter: bool
    autonomous_buffer_recovery: bool


# Exact entries deliberately exclude future/unknown versions. The optional MCU
# command 0x12 does not replace the stock box's staged 0x10 operation.
_PROFILES = {
    "1.1.3": FirmwareProfile("1.1.3", 4, False, False, False),
    "1.2.2": FirmwareProfile("1.2.2", 4, False, False, False),
    "1.4.2": FirmwareProfile("1.4.2", 6, True, False, False),
    "1.5.0": FirmwareProfile("1.5.0", 6, True, True, True),
    "1.5.2": FirmwareProfile("1.5.2", 6, True, True, True),
    "1.5.3": FirmwareProfile("1.5.3", 6, True, True, True),
}


@dataclass(frozen=True)
class Identity:
    version: str
    sn: str
    version_digits: str
    raw_data: bytes
    raw_reply: bytes


def parse_identity(data, raw_reply=None):
    """Validate a payload without changing device context or inferring family."""
    if not isinstance(data, _BUFFERS) or (
        raw_reply is not None and not isinstance(raw_reply, _BUFFERS)
    ):
        raise ValueError("CFS identity must be supplied as byte buffers")
    payload = bytes(data)
    raw = bytes(raw_reply) if raw_reply is not None else payload
    if len(payload) != 22 or any(not 48 <= value <= 57 for value in payload[:3]):
        raise ValueError("CFS identity needs three ASCII digits and nineteen serial bytes")
    digits = payload[:3].decode("ascii")
    return Identity(".".join(digits), payload[3:].decode("latin1"), digits, payload, raw)


def _address(address):
    if type(address) is not int or address not in range(1, 5):
        raise ValueError("CFS address must be 1 through 4")
    return address


def _family(family):
    if family is not None and not isinstance(family, str):
        raise ValueError("CFS hardware family must be a string or None")
    return family


class DeviceVersions:
    """Keep observed identity separate from externally confirmed hardware.

    The 0x14 payload carries three ASCII version digits and nineteen serial
    bytes. It has no family/hardware identifier. Declaring ``families`` is
    explicit caller context; a serial-number replacement clears that context.
    A disconnect requires a new identity read, even for a previously known box.
    """

    def __init__(self, families=None):
        self._families = {
            _address(address): _family(family)
            for address, family in (families or {}).items()
        }
        self._identities = {}
        self._last_identities = {}
        self._raw_replies = {}
        self._generations = {}

    def get(self, address) -> Optional[Identity]:
        return self._identities.get(_address(address))

    def generation(self, address):
        return self._generations.get(_address(address), 0)

    def last_raw_reply(self, address):
        """Return the last supplied bytes for diagnostics, not a live identity."""
        return self._raw_replies.get(_address(address))

    def _advance(self, address):
        self._generations[address] = self.generation(address) + 1

    def invalidate(self, address, raw_reply=None):
        """Drop current identity while retaining the replacement fingerprint."""
        address = _address(address)
        if raw_reply is not None:
            if not isinstance(raw_reply, _BUFFERS):
                raise ValueError("CFS raw identity reply must be a byte buffer")
            self._raw_replies[address] = bytes(raw_reply)
        self._identities.pop(address, None)
        self._advance(address)

    def set_family(self, address, family):
        address, family = _address(address), _family(family)
        if self._families.get(address) != family:
            self._families[address] = family
            self._advance(address)

    def profile(self, address) -> Optional[FirmwareProfile]:
        address = _address(address)
        identity = self.get(address)
        if identity is None or self._families.get(address) != "cfs0":
            return None
        return _PROFILES.get(identity.version)

    def require_supported(self, address):
        profile = self.profile(address)
        if profile is None:
            raise ValueError("No confirmed supported CFS identity at address %s" % address)
        return profile

    def update(self, address, data, raw_reply=None):
        """Accept an identity payload, invalidating old selection on malformed data.

        ``raw_reply`` is the full validated frame when available; when omitted,
        diagnostics retain the payload itself. Serial bytes follow the stock
        byte-to-character conversion, including embedded NUL and high bytes.
        """
        address = _address(address)
        if isinstance(raw_reply, _BUFFERS):
            self._raw_replies[address] = bytes(raw_reply)
        elif raw_reply is None and isinstance(data, _BUFFERS):
            self._raw_replies[address] = bytes(data)
        try:
            identity = parse_identity(data, raw_reply)
        except ValueError:
            self.invalidate(address)
            raise
        previous = self._last_identities.get(address)
        if previous is not None and previous.sn != identity.sn:
            self._families.pop(address, None)
            self._advance(address)
        elif previous is not None and previous.version != identity.version:
            self._advance(address)
        self._identities[address] = identity
        self._last_identities[address] = identity
        return identity
