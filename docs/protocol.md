# Protocol and native serial contract

English | [Русский](protocol.ru.md)

**Status:** draft reference, not a complete K1C protocol specification. Offline
framing checks, static API review and target hardware validation are different
levels of evidence. The current package performs no serial exchanges.

Stable section anchors allow implementation and tests to reference this document.
Add a verified target command here before using it in an operation.

## Boundaries and evidence

Box operations will use the existing Klipper serial object through its verified
API. The stock library performs transport work and framing. This
document describes both that API boundary and the wire format; it does not
introduce an independent transport implementation.

| Label | Meaning |
| --- | --- |
| Verified offline | A reproducible check of the specified data or code path; not a hardware result |
| Partial | Some evidence exists, but target version or behavior is not fully established |
| Unknown | No adequate evidence for the stated target behavior |

Scope each claim to the host/MCU versions and operation actually examined.
Matching an opcode alone is insufficient. Original binaries and research
references remain outside public source at this stage.

<a id="firmware-versions"></a>

## Firmware version requirements

The current baseline is CFS MCU 1.1.3; 1.5.2 and 1.5.3 are additional targets
awaiting hardware validation. Static comparison also covers 1.2.2, 1.4.2 and
1.5.0 cfs0/G32 images. Version detection and selection are not implemented.

Before relying on version-specific behavior, read the running firmware through
the retained native API and keep its raw version/identity per box. A bundled
image name or update manifest is not proof of what the MCU is running. Keep the
MCU version distinct from the printer firmware and `[box] version` setting.

The examined `0x14` reply carries three ASCII version digits and 19 serial-number
bytes. It does not identify the product generation or hardware ID by itself.
Record hardware identity separately and select behavior per connected device.

Offline comparison establishes four ordinary `0x0A` data bytes in 1.1.3/1.2.2
and six in 1.4.2/1.5.0/1.5.2/1.5.3. Slot-event replies with status `0x30` retain
four data bytes. Additional ordinary fields must not be mistaken for events.
The examined 1.5.2/1.5.3 applications share ordinary command behavior; factory
test parameters and version metadata differ. This is not hardware acceptance.

The [baseline and firmware change history](compatibility.md#firmware-history)
describe the cumulative changes from the reference CFS MCU 1.1.3 through 1.5.3,
including RFID, buffer recovery and the additional feed command.

For each validated version, record request/reply layouts, status meanings,
preload and staged-operation behavior, and applicable wait/retry limits. Decode
ordinary replies and event replies separately. Matching request lengths do not
prove matching response lengths or mechanical behavior.

Enable additional commands only after verifying both MCU support and their
behavior through the retained `serial_485.so`, including its command-dependent
retries. Unknown firmware must remain explicitly unsupported for mechanical
operations until the required behavior is established. Tests and hardware
acceptance must identify the exact version combination they cover.

<a id="framing"></a>

## Frame format

**Evidence: partial for the target.** Frame geometry and CRC have been checked
offline against saved reference data. Target native-API operation and physical
completion have not been verified by this package.

| Field | Size | Meaning |
| --- | --- | --- |
| SYNC | 1 byte | `0xF7` |
| ADDR | 1 byte | Destination/reply address; addressing context matters |
| LEN | 1 byte | Number of following bytes: STATUS + CMD + DATA + CRC |
| STATUS | 1 byte | Request context or response status; command/mode dependent |
| CMD | 1 byte | Command within that address/mode context |
| DATA | N bytes | Command-specific payload |
| CRC | 1 byte | CRC-8 of LEN through DATA |

`LEN = N + 3`; total frame size is `N + 6`, or `LEN + 3`.
CRC parameters: polynomial `0x07`, initial value `0x00`, no reflection, final
XOR `0x00`. SYNC and ADDR are excluded from the CRC scope.

Synthetic framing example: `F7 01 03 00 A2 DA`. It contains no DATA;
CRC over `03 00 A2` is `DA`. This example demonstrates geometry only and is
not an instruction to send an addressing command to equipment.

The command catalogue, slot encoding, payload units/endianness and response
semantics must be verified for the target version before implementation. The
catalogue is not populated with unverified mechanical commands.

<a id="native-api"></a>

## Native API

**Evidence: partial, based on static review.** Recheck these details against
the exact retained native library before implementing native calls.

| Item | Reviewed boundary | Remaining verification |
| --- | --- | --- |
| Object | Existing Klipper object for the configured `serial_485` bus | Initialization order and availability on the target |
| Request method | `cmd_send_data_with_response(data, timeout, retries_en=True)` | Exact behavior for each required command |
| Request bytes | ADDR, LEN, STATUS, CMD, DATA; native code adds SYNC/CRC | Validate target command and length handling |
| Reply | Full framed bytes on response, `None` after exhaustion; exceptions may propagate | Late replies, negative status and disconnect paths |
| Retries | Native code may repeat selected commands even with `retries_en=False` | Actual transmission count and timeout budget per command |
| Receive access | Native receive admission and callbacks are constrained | Which observations the supported request path exposes |

Callers must distinguish no response, rejected/malformed response, acknowledged
request and physical completion. A non-`None` reply is not an operation success.
Do not assume the retry flag guarantees a single transmission or callbacks expose
every incoming frame. Keep borrowed connection ownership with Klipper.

## Operation coverage

The complete specification covers initialization, connection, loading, unloading,
cutting, tool changes, purging, pause/resume, failures/recovery, sensors, RFID and
spool data. Each group needs its own verified commands, observations, waits and
failure paths. The loading section below is one part of that full contract.

<a id="loading"></a>

## Loading

**Evidence: unknown for a complete replacement route.** Before implementation,
fill in the exact commands, predicates, deadlines and failure exits below from
the target original. The table describes required coverage, not a recovered
wire sequence.

| Phase | Required information |
| --- | --- |
| Preconditions | Initial filament location, slot presence, readiness and temperature |
| CFS feed | Native calls and accepted/rejected reply handling |
| Arrival | Separate CFS, buffer and toolhead observations; treatment of missing data |
| Extruder feed | Coordination with existing macros, modes and configured lengths |
| Purge | Existing configured behavior and completion conditions |
| Completion | Fresh observations that establish success and the resulting state |
| Failure | Stop/release behavior, reported error, retained uncertainty and permitted retry |

Until that reconstruction is complete, do not invent stage timing or copy a
sequence from a different printer/MCU. Keep the existing preload policy;
configuration such as OPEN and an action such as RUN are not interchangeable.
RFID or slot refresh can involve mechanics and is not a passive status query.

<a id="timing"></a>

## Timing, retries and failures

Record transaction timeout, possible native retransmissions, total operation
deadline, expected reply hold and sensor wait separately. Use the reactor's
monotonic clock and cooperative waits when implemented. Numerical budgets and
recovery actions remain open until verified for the target route.

Unknown sensor state must not become a successful load. A configured material,
cached active slot or ACK must not override contradictory physical observations.
Track implementation coverage in [compatibility.md](compatibility.md).
