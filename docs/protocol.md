# Protocol and native serial contract

English | [Русский](protocol.ru.md)

**Status:** borrowed-native calls, reply decoding, per-device version selection,
and stage 4 lifecycle, error and operation-context components are tested with PC
substitutes. The package is `cfs`; the Klipper object remains `box`. The bootstrap still
refuses activation. These checks did not execute the retained `.so` or contact
the printer; a complete K1C replacement cycle remains unverified.

Stable section anchors allow implementation and tests to reference this document.
Add a verified target command here before using it in an operation.

## Boundaries and evidence

The implemented pipeline borrows the configured Klipper serial object through
the recovered API. The stock library performs transport work and framing. This
document describes both that API boundary and the wire format; it does not
introduce an independent transport implementation.

| Label | Meaning |
| --- | --- |
| Verified offline | A reproducible check of the specified data or code path; not a hardware result |
| Partial | Some evidence exists, but target version or behavior is not fully established |
| Unknown | No adequate evidence for the stated target behavior |

Scope each claim to the host/MCU versions and operation actually examined.
Matching an opcode alone is insufficient. Original binaries and private research
remain outside public source. The [contract matrix](contract-matrix.md) links
approved official factory references and binds the full host interface to checks.

<a id="native-integration"></a>

## Implemented native integration

Explicit binding looks up `serial_485 <configured bus>` and borrows that object.
It does not open UART, connect/disconnect native serial, or take over its queues
and timers. Its transport connection flag does not establish CFS presence.
`Request.body` contains `ADDR LEN REQUEST_STATE CMD DATA`; ordinary typed
requests use state `0xFF`. The stock library adds F7/CRC for transmission.
`Frame` retains the full reply and validated
address, command, status and payload. Bad header, length, CRC or attribution is
an error, distinct from `None` and a negative MCU status.

Three retry layers remain separate:

| Owner | Trigger and scope |
| --- | --- |
| Box sender | For ordinary requests, valid MCU CRC/LENGTH errors cause up to seven native API exchanges below timeout 3600, three at/above it |
| Stock wrapper | Selected commands have their own outer attempts on `None`, up to five; `retries_en=False` does not remove this loop |
| Stock serial handler | Its wait/retry path uses up to five attempts when retries are enabled and timeout is below 3600; otherwise one, with a lower wait of one second at/above 3600 |

Ordinary calls supply exactly `(body, timeout)`; an explicit retry flag remains
an optional third argument. Only the box-level exchanges are implemented here;
the two retained native loops were reconstructed statically. One API call is
not one UART transmission, and these counters are not an operation deadline.
`COMMUNICATION_TEST` uses its separate response-byte path, including integer
zero, and bypasses MCU-status retries.

Binding and per-address generations are checked before every native API attempt,
after waiting or failure, and before/after each ordered semantic effect. A stale
context stops subsequent host processing; deferred callbacks also reject old
context after detachment. These guards do not interrupt an API call already in
progress or cancel its internal native retries.

The lifecycle supplies shared reporting, saved-error and timeout handlers, and
retains slot events as diagnostics. Other required operation hooks remain unbound;
their absence raises `MissingProtocolEffect` before effect execution. The sender
retains contextual `True`/`False`/`None` outcomes. ACK, telemetry and cached labels
do not establish completed loading or switching.

Execution gates block all opcode `0x13` requests, typed legacy encoding-0
`EXTRUDE_PROCESS`, typed stages above 7, and unaddressed diagnostics (`ADDR=0`).
Confirmed wire stages are 0–7. Their mapping to public STAGE values, host/log
phase names and full operations remains a separate open question. Raw diagnostics
do not turn an unknown payload into a verified typed operation, and optional
`0x12` is never selected automatically.

The [native](../tests/test_native.py), [decoder](../tests/test_protocol.py),
[firmware](../tests/test_firmware.py) and [integration](../tests/test_box_native.py)
tests use synthetic frames and substitutes. Real native admission, transmission
counts, scheduling and mechanical completion remain hardware acceptance work.

<a id="lifecycle-integration"></a>

## Lifecycle, errors and operation context

Explicit registration provides 46 functional handlers and two conditional Hall
handlers. On `klippy:ready`, the
lifecycle resolves its dependencies, borrows serial, emits `box:ready` and then
starts its own heartbeat timer. Presence uses the address manager's `online == 1`
and the serial port's existence, independently of the native connection flag.
On connection it deliberately reads identity before `0x0A`, so the latter uses
the correct profile. Reconnection requires a fresh identity; a different serial
number clears family confirmation. Initial saved slot metadata enters live state
only after a fresh successful MATERIAL sensor response.
Confirming a previously unknown family or changing a known version forces a
fresh startup attempt before ordinary heartbeat polling resumes.

Reported modes `0`–`5` mean IDLE, PRELOADING, PRINTING, WRAPPERING, ERROR and
TEST respectively. They are not the action encoding sent by `SET_BOX_MODE`.
The heartbeat polls one eligible address per pass; `None` reduces its counter
from five, while `False` and event responses reset it. Startup waits at most ten
half-second intervals for a `None` power-on preload setting; `False` ends the
wait. A terminal heartbeat exception stops that timer without automatic restart.
Shutdown removes extension-owned work and unbinds the borrowed object; it does
not close the native transport. Binding, device and operation generations stop
stale follow-up work after rebind or shutdown, including after G-code and waits.

The configured head sensor is used consistently by aggregate state, CFS enable
and filament-error handling. With no configured sensor, presence uses the
stock `True` fallback and these paths emit no sensor G-code. Timeout reporting
is suppressed if the saved address state is not `connect` and the native link
is connected. Otherwise it reports; only a previously connected address is
set to `disconnect`.

Error messages preserve the client schema, duplicate suppression and special
keys that report without pausing. Ordinary reporting exceptions are logged and
the flag is cleared. `inside_error` is an observable processing flag, not a
re-entry rejection. Printing errors can pause and restore saved state; checks
after cooperative actions stop stale continuation. The operation context owns
the target and temporarily disables heartbeat only for that address while the
head phase is armed. It records the stage-6 result and status even when the
result is `False`; neither the flag nor an ACK proves physical completion.
Missing or raising phase actions cancel ownership and restore the previous
heartbeat permission. The loading phase consumes this saved result. Public tests use an own producer fixture. Separate local checks execute the
pinned retained SwitchSensor/RunoutHelper inside a native stage-four substitute
and prove complete loading consumes a saved False result. Disabled, debounced
and duplicate helper input still emits the custom event. No native binary or
physical sensor ran.

`slot_events` holds per-address diagnostics; the slot event executor separately
updates metadata and schedules guarded refreshes. Power-on refresh, head motion
and stage 6 use real supplied actions and fail explicitly when needed but absent.
Tool changes and recovery have synthetic PC checks, not hardware acceptance. See [compatibility](compatibility.md) and the
[lifecycle](../tests/test_lifecycle.py), [error](../tests/test_errors.py) and
[operation](../tests/test_operations.py) component tests.

<a id="firmware-versions"></a>

## Firmware version requirements

The current baseline is CFS MCU 1.1.3; 1.5.2 and 1.5.3 are additional targets
awaiting hardware validation. Static comparison also covers 1.2.2, 1.4.2 and
1.5.0 cfs0/G32 images. The code contains exact profiles for **1.1.3, 1.2.2,
1.4.2, 1.5.0, 1.5.2 and 1.5.3**, with no fallback for an unknown version.

Before relying on version-specific behavior, read the running firmware through
the retained native API and keep its raw version/identity per box. A bundled
image name or update manifest is not proof of what the MCU is running. Keep the
MCU version distinct from the printer firmware and `[box] version` setting.

The examined `0x14` reply has exactly 22 data bytes: three ASCII version digits
and 19 serial-number bytes. Serial bytes use the stock byte-to-character Latin-1
representation, including NUL and high bytes, without trimming. Raw reply bytes
and the exact dotted version are retained separately for each address.
This identity does not identify the product family or hardware ID. A supported
profile requires separately confirmed `cfs0` context and an exact known version;
`[box] version` is not that confirmation.

Malformed or failed identity reads invalidate the current profile and public
version/SN while retaining raw diagnostics. Disconnect/rebind invalidates current
identity and requires a fresh read. After disconnect, the same serial can reuse
its explicit family context; a replacement serial clears that confirmation.
Unknown families or firmware never inherit baseline mechanical behavior.

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

The matrix records all 19 host command identifiers and the 16 typed request
schemas. Reserved identifiers are kept separate from implemented typed
operations. Mechanical units and stage transitions require their own acceptance.

<a id="native-api"></a>

## Native API

**Evidence:** recovered from the retained library and exercised through PC
substitutes. Actual target ABI, admission and timing remain unverified.

| Item | Reviewed boundary | Remaining verification |
| --- | --- | --- |
| Object | Existing Klipper object for the configured `serial_485` bus | Initialization order and availability on the target |
| Request method | `cmd_send_data_with_response(data, timeout, retries_en=True)` | Target ABI and actual behavior for each required command |
| Request bytes | ADDR, LEN, STATUS, CMD, DATA; native code adds SYNC/CRC | Validate target command and length handling |
| Reply | Full framed bytes on response, `None` after exhaustion; exceptions may propagate | Late replies, negative status and disconnect paths on the target |
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

**Evidence: unverified for a complete replacement route.** Use the matrix's
recovered commands, predicates and failure exits when implementing the route.
The table describes required operation coverage, not a successful runtime cycle.

| Phase | Required information |
| --- | --- |
| Preconditions | Initial filament location, slot presence, readiness and temperature |
| CFS feed | Native calls and accepted/rejected reply handling |
| Arrival | Separate CFS, buffer and toolhead observations; treatment of missing data |
| Extruder feed | Coordination with existing macros, modes and configured lengths |
| Purge | Existing configured behavior and completion conditions |
| Completion | Fresh observations that establish success and the resulting state |
| Failure | Stop/release behavior, reported error, retained uncertainty and permitted retry |

Do not invent stage timing or copy a sequence from a different printer/MCU.
Keep the existing preload policy;
configuration such as OPEN and an action such as RUN are not interchangeable.
RFID or slot refresh can involve mechanics and is not a passive status query.

<a id="timing"></a>

## Timing, retries and failures

Record transaction timeout, possible native retransmissions, total operation
deadline, expected reply hold and sensor wait separately. Use the reactor's
monotonic clock and cooperative waits for operation coordination. Request
timeouts are encoded and PC-tested; actual operation deadlines and recovery
timing remain unverified for the target route.

Unknown sensor state must not become a successful load. A configured material,
cached active slot or ACK must not override contradictory physical observations.
Track implementation coverage in [compatibility.md](compatibility.md).
