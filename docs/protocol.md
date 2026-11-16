# Protocol and native serial contract

English | [Русский](protocol.ru.md)

The package is `cfs`; the Klipper object remains `box`. It borrows the stock
`serial_485` object and starts in passive identity/telemetry mode. Reference CFS
MCU 1.1.3 validation includes four preload checks and two consecutive complete
T1C load/unload cycles without manual assistance.

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

The lifecycle supplies reporting, saved-error, timeout, slot-event and runout
handlers. A missing required handler raises `MissingProtocolEffect` before
effect execution. The sender
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
tests cover the API boundary, frames and version selection.

<a id="lifecycle-integration"></a>

## Lifecycle, errors and operation context

`load_config` always registers passive admission: by default only typed reads `0x02`, `0x03`,
`0x05`, `0x08`, `0x0A` and `0x14` may reach the borrowed native API. Raw diagnostics,
wheel requests, `CREATE_CONNECT` and writes are blocked, as are host actions.
Automatic preload/RUN, sensor toggles, head feed, runout tightening, pause and
heating are suppressed. Reports retain their client text without executing
mechanical error tails. There is no general mechanics switch. The separate
[explicit preload trial](installation.md#single-operation-trial) can grant one
selected `0x0D` RUN without changing that passive lifecycle; its request and
dispatch ownership do not apply to ordinary commands or background callbacks.
See [startup dependencies and data paths](installation.md#passive-startup).

On baseline MCU 1.1.3, `GET_RFID` (`0x02`) reads the available per-slot record;
it does not perform physical RFID acquisition. A present slot without an
available record can return `unknown`. In that case, `GET_REMAIN_LEN` (`0x03`)
returns the fallback byte `100`, which does not confirm a full spool or a fresh
remaining-material estimate. Both replies were observed on the reference unit.
The host preserves known colours/materials while recording `vender=unknown`;
these getter replies can therefore replace previously saved metadata. The full
RFID acquisition/estimation path can move motors and is not started by default
passive admission. A saved tag ID or remaining value must be distinguished from a fresh
MCU observation.

For ordinary baseline 1.1.3 `0x0A` replies, `DATA[2]` contains pending motor
diagnostics consumed by the state read; mode is the separate `DATA[3]`.
Bits 0–3 identify channels A–D, bit 4 represents the shared low-motion monitor,
and bit 7 marks delivery of a persistent condition.
A background heartbeat can consume an indication before another read.
These flags are separate from physical switches and operation completion.

The following operation policies describe the active component contract tested
with explicit PC construction. Registration provides 101 unconditional commands
and two conditional Hall commands. On `klippy:ready`, the
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
heartbeat permission. The loading phase consumes this saved result. The custom
head event remains separate from RunoutHelper debounce, disable and duplicate
filtering.

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

Frame geometry and CRC are checked on the PC and in baseline API replies.

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

The retained API is exercised through PC substitutes and the unchanged reference
library on MCU 1.1.3.

| Item | Reviewed boundary | Remaining verification |
| --- | --- | --- |
| Object | Existing Klipper object for the configured `serial_485` bus | Ownership and availability across other configurations/failure paths |
| Request method | `cmd_send_data_with_response(data, timeout, retries_en=True)` | Per-command behavior outside the observed baseline paths |
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

## Loading and unloading

The [explicit load/unload trial](installation.md#material-operation-trial) owns
one selected slot and validates fresh head, hub, buffer, mode and source state.
It executes the configured macro recipe, including heating, cutting, cleaning,
flush and position restoration, while the default lifecycle stays passive.

### Preload

OPEN configures power-on preload; RUN performs it. Full power-on refresh runs
before RFID/remaining queries. Passive admission permits typed reads and
suppresses automatic RUN and slot-event actions. The
[preload trial](installation.md#single-operation-trial) checks baseline mode and
fresh MATERIAL/CONNECTIONS before one selected RUN, then requires a normal IDLE
reply before selected-slot RFID/remaining reads. RFID validation rejects
additional or duplicate records before metadata persistence.

### Full LOAD

Loading admits typed `0x0F`, selected `0x10` stages 0/4/5/6/7, `0x04` PRINT and
fresh `0x0A`/`0x08`/`0x05` observations. Stage 6 belongs only to the armed head
callback. Initial admission accepts neutral BUFFER `0` or empty-limit `2` and
rejects `1`/`3`; neutral means neither limit is active.

Successful baseline 1.1.3 `0x10` replies have stage-specific DATA:

| Request stage | DATA size | Successful reply data |
| --- | --- | --- |
| `0` | 1 byte | Mode `0` after normal initialization |
| `5` | 4 bytes | Measuring-wheel float32, big-endian |
| `4`, `6`, `7` | 0 bytes | Empty payload |

Stage `4` acknowledges before its feed loop completes. Head arrival, stage `6`
and subsequent observations complete that path. Replies carry no stage echo
or transaction ID; stage-specific body checks reject incompatible responses.

The owned LOAD supplies `LEN=int(cfg.Tn_extrude)` to its flush command (150 mm
in the reference configuration). Admission requires an integral length 1–200 mm,
finite positive velocity/percent pairs, effective positive feed at most 200 mm,
and actual speed/extrusion factors of 1. A false head-feed result stops before
cleaning. Standalone commands retain their configured defaults.

### Full UNLOAD

Known-source UNLOAD uses the configured cutter and `0x04` IDLE / `0x11`
BUFFER/MATERIAL path. Inside the owned trial its retraction sequence is:

1. Complete selected BUFFER and the blocking E−20 head assist.
2. Queue finite E−30 F600, then submit step generation without waiting.
3. Send one selected MATERIAL request while the head tail may still execute.
4. After a normal ACK, complete `M400` before source cleanup and XYZ return.

Hot-heater, scale and operation-ownership checks gate the coordinated tail.
An error revokes admission; it does not retry or return XYZ. Already submitted
finite extrusion may finish. This coordinates the two withdrawals without
assuming identical physical motor start times. Ordinary and unknown-source
paths retain their configured sequences.

Unknown-source UNLOAD keeps both host source hints absent. Within its current
retraction command it admits, in order: fresh `0x08` CONNECTIONS, `0x08` MATERIAL,
`0x0A` STATE, `0x04` IDLE, `0x11` BUFFER with selector zero, then selected-lane
`0x11` MATERIAL. Each normal response advances one step; mask conflicts,
missing/abnormal replies, reordering or duplicates stop the path. The shared
selector is limited to this command and phase.

Full UNLOAD requires fresh mode `0`, selected material present, CONNECTIONS zero,
BUFFER `2`, head absent, cutter released and source cleared. ACKs and cached
labels are checked separately from these observations and from nozzle flow.

### CFS-only retraction

The [native-only trial](installation.md#native-buffer-trial) defaults to one
selected `0x11` BUFFER after IDLE `0x04`/`0001` and fresh normal STATE. Explicit
`TRIGGER=MATERIAL` selects MATERIAL instead; it is not an automatic fallback.
Neither form performs CUT, XYZ/E or heating. Only initial STATE status `0x13`
(19) or `0x19` (25), with four data bytes and mode `4`, is admitted as an error
observation; subsequent replies must be normal. MATERIAL requires fresh mode
`0`, CONNECTIONS zero, BUFFER `2` and head absent after its normal ACK.

Baseline BUFFER status `0x13` means that the empty-buffer condition was not
confirmed within its approximately five-second firmware wait. MATERIAL status
`0x19` is `RETRUDE_ERR6`.
A valid CRC preserves these MCU error meanings; it does not turn them into
successful retraction.

All explicit trials retain dispatch/command ownership and duplicate-event gates.
They admit no raw diagnostics, another CFS or automatic recovery. Host CRC/LENGTH
resends are disabled; revoking host permission cannot cancel an in-flight MCU
operation or the retained library's internal retries.

<a id="timing"></a>

## Timing, retries and failures

Record transaction timeout, possible native retransmissions, total operation
deadline, expected reply hold and sensor wait separately. Use the reactor's
monotonic clock and cooperative waits for operation coordination. Request
timeouts are encoded and PC-tested; normal baseline trial observations do not
validate deadline expiry, in-flight cancellation or recovery timing on target.

Unknown sensor state must not become a successful load. A configured material,
cached active slot or ACK must not override contradictory physical observations.
Track implementation coverage in [compatibility.md](compatibility.md).
