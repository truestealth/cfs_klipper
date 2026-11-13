# Full contract matrix

English | [Русский](contract-matrix.ru.md)

The [machine-readable matrix](contract-matrix.json) contains **423 requirements**
and **96 check families**: 78 on PC and 18 on hardware. Stage 9 records
59 implemented families, 19 partial and 18 planned; every planned
family is a hardware check. Public synthetic fixtures and additional local
retained-source execution have distinct evidence scopes. Catalogue integrity
alone does not establish functional compatibility.

The baseline is CFS MCU **1.1.3**, with the retained stock `serial_485.so` and
the reference K1C CFS Upgrade configuration. Differences between known MCU
versions are represented in the shared implementation.

## Inventory

| Surface | Requirement rows | Scope |
| --- | ---: | --- |
| G-code | 103 | 71 named commands: 69 unconditional and two conditional Hall commands; 32 individual T commands |
| Status and internal compatibility state | 64 | 28 status/API rows and 36 mapping, observation and operation-context rows |
| Configuration and endpoint | 76 | Every one of 54 loader options; the endpoint API and its 21 individual fields |
| Persistence | 14 | Stored keys, files, cleanup and failure outcomes |
| Native boundary | 52 | Eight API/ownership requirements, all 19 host command identifiers and all 25 decoded statuses |
| Operations and errors | 49 | 29 operation routes, 19 ordinary/macro error families and the client error-report API |
| Dependencies and events | 31 | 28 dependency boundaries, including retained Python consumers and `chelper`, and three registered events |
| Firmware variants | 6 | 1.1.3, 1.2.2, 1.4.2, 1.5.0, 1.5.2, 1.5.3 |
| Existing macro consumers | 26 | Required preservation and interaction contracts, including grouped external macro families |
| Installation and build | 2 | Reversible Klipper layout and checkout-based Python checks |

A row is a requirement, not necessarily a command or a separate test function.
`Tn` status/sensor templates explicitly cover T1 through T4; slot arrays use
A/B/C/D order. The templated status covers 69 concrete key paths, including
the four box containers. Each configuration option and endpoint field has its
own row. Loader defaults describe the examined implementation and must not
overwrite existing printer settings.

## Reading and maintaining the matrix

The JSON catalogue uses English technical statements shared by both language
versions of this guide. Stable IDs connect requirements, check cases and open
questions:

| Field | Meaning |
| --- | --- |
| `id`, `kind`, `name` | Stable identifier, surface and interface/behavior name |
| `parameters`, `when` | Signature/defaults/types/bounds and applicable configuration or registration condition |
| `contract` | Required observable behavior or preserved ownership boundary |
| `stage` | Implementation stage from the [plan](implementation-plan.md), not a wire-stage number |
| `checks` | PC/HW case IDs; each resolves to concrete assertions in the catalogue |
| `evidence` | `static`, `partial` or later `hardware`; this is independent of implementation |
| `implementation` | `pending`, `partial` or `implemented`; component status is separate from hardware acceptance |
| `implementation_note` | Current implementation/evidence scope or a deliberate difference from the original |
| `blockers` | Applicable unresolved questions, with affected rows and closing criteria in `gaps` |
| `code`, `alias_of` | Numeric wire/status value or a registered alias, where applicable |

For example, `CMD-BOX_SET_PRE_LOADING` maps to parameter, native-call and
preload checks. `MACRO-INFO-REFRESH` adds the requirement that a consumer's
refresh invokes preload RUN. `OP-HEAD-CALLBACK` binds the stage4/stage6
interaction to callback timing, failure and hardware cases.

Check families use `status: planned` with `test: null`, or `partial`/`implemented`
with a list of runnable references such as
`tests/test_state.py::StateTests.test_raw_getters_and_distinct_sentinels`.
A partial family also records `remaining` assertions. The catalogue test verifies
that each reference resolves to an actual method; executing the suite establishes
its local result. Do not create empty tests, mark a retained macro as newly
implemented, or count registration as working mechanics.

The matrix records 316 implemented requirements and 107 partial requirements.
Partial rows identify concrete unsupported paths or incomplete PC assertions;
hardware acceptance remains separate. Every row has a current implementation note. The [compatibility notes](compatibility.md#state-configuration-and-storage)
describe cache/observation separation, error behavior and deliberate save rules;
[native integration](protocol.md#native-integration) records the implemented
boundary and execution gates. The [lifecycle notes](protocol.md#lifecycle-integration)
describe the new components and deliberate policies. Handler registration alone
does not complete shared contracts: 101 commands are explicitly registered,
including one blocked legacy command, plus two conditional Hall handlers. The 32
generated bindings have an isolated dispatch check. Separate composed PC tests
exercise real Box phases with synthetic native replies, including cross-box
changes, print cleanup and purge failures. Jinja tests use own public templates;
pinned retained-source execution is additional local evidence, not public CI.
A family validation_note states that distinction where applicable.

## Macro integration found during the inventory

The matrix includes the active definitions in the examined configuration copies,
their include/override order, renamed command aliases, parameter forwarding,
macro variables and consumers of `printer.box`:

| Interaction | Required check |
| --- | --- |
| `rename_existing` | Base commands exist before `klippy:connect` installs the macro aliases; preserve include/override order |
| `NEXT_FILAMENT` and the two `_99` wrappers | Preserve the temperature variable and exact `rawparams` forwarding through extruder/flush wrappers |
| Material-present/absent load and quit macros | Keep their distinct ordered phase calls and existing error/material waits |
| `BOX_INFO_REFRESH ADDR=... NUM=...` | Preload RUN precedes RFID/remainder requests; this is mechanically active, and `M400` is not physical-completion evidence |
| Active `START_PRINT` | Preserve `INITIAL_TOOL` validation, heating before T, initial purge and the existing startup override |
| End/cancel | Preserve `BOX_END` before `BOX_END_PRINT`, the shared cleanup and retained base aliases |
| Pause/resume | Preserve `POST_WORK=0`, `BOX_ERROR_CLEAR` then `BOX_RESUME_EXTRUDE`, existing variables and forwarded resume velocity |
| Runout definitions | Preserve the captured primary/head routing, including `printer.box.enable` ownership selection |
| Template execution | Render-time snapshots differ from the subsequent execution of emitted G-code; nested macro calls have their own boundary |
| Saved diagnostic status consumer | Preserve aggregate `state` before nested states and the observed T1–T4 version order for this particular consumer |

These are contracts with existing macros, material/diagnostic helpers and sensor
definitions. Their files and services remain with their current owners. Existing
macro names must not be registered as replacement-owned commands just because
they contain `BOX_`. Saved copies establish the examined content; the active
printer configuration must be identified before activation/hardware acceptance.
Both examined saved load wrappers omit required physical `TNN`; the original
and replacement handler fail on that path. `GAP-MACRO-LOAD-TARGET` remains open,
with no guessed target and no automatic edits to user macros.

## Direct Python consumers

Retained printer modules also access the box object directly. The examined
target Python source inventory identifies these required paths:

- `box.box_state.is_continue_print`;
- `box.box_action.box_state.Tn_data['enable']`;
- `box.box_action.box_save.move_z` and `.last_err`;
- `box.record_tn_move_z(value)`, including the caller supplying zero;
- `box.box_state.tn_save_data_path`;
- `box.inside_error`, observed by the retained cancel wait;
- `box.box_action.heart_process_enable`, observed by the address manager.

The deferred resume target belongs to **`virtual_sdcard.resume_tnn`**.
The Z-recording hook delegates print-file metadata with `tn_move_z`; it is
separate from motion. Preserve these paths and their shared read/write state
without requiring a copy of the original class hierarchy. Status deep copies
must not replace externally writable live state. `PC-RETAINED-BOX-API` records
the corresponding acceptance assertions.

Client-facing errors have a separate `ERROR-REPORT-API` requirement: retain
`!! {code,msg,values}`, duplicate/special-key handling, the error guard and the
context-specific pause/after-pause effects.

## Execution decisions and open questions

| Gap | Required resolution |
| --- | --- |
| `GAP-LEGACY-13` | Establish callers and behavior for the host two-byte request versus the MCU one-byte request; make the compatibility decision before executing that route |
| `GAP-STAGES` | Verify public-to-wire conversion and legacy triggers; public STAGE bounds do not establish accepted wire-stage numbers |
| `GAP-OPTIONAL-POSITIONS` | The empty endpoint fallback is recovered and PC-tested; check remaining movement/resume paths with positions enabled and disabled |
| `GAP-SERIAL-NAME` | Resolved: use only the configured bus; omit the historical redundant fixed-name lookup. Baseline and alternate names are PC-tested |
| `GAP-HEAD-SENSOR-NAME` | Resolved: use the configured sensor for lookup and every enable path. Without a configured sensor, retain the distinct True fallback and emit no sensor-enable G-code |
| `GAP-RESERVED-WIRE` | Keep three reserved identifiers separate from invented direct APIs; recover a required path before using it |
| `GAP-MACRO-SNAPSHOT` | Identify the active include chain and configuration fingerprints before printer activation/acceptance; saved copies and temporary-tree deployment do not close it |
| `GAP-MACRO-LOAD-TARGET` | Resolve the missing physical TNN in both examined load wrappers through an explicit caller target or agreed policy; no inferred spool or silent macro rewrite |

Five gaps remain open; serial and head-sensor lookup policies are resolved. Open gaps gate
their affected paths. Recording a gap completes the inventory entry; it does not
complete implementation or grant a mechanical path success.

## Factory reference and target evidence

The official [K1 Series Klipper repository](https://github.com/CrealityOfficial/K1_Series_Klipper/tree/e09f36e6ada60e5467b0bef731a96263b5d8095b)
is pinned at `e09f36e6ada60e5467b0bef731a96263b5d8095b`. Its published
[macro engine](https://github.com/CrealityOfficial/K1_Series_Klipper/blob/e09f36e6ada60e5467b0bef731a96263b5d8095b/klippy/extras/gcode_macro.py)
establishes the `klippy:connect` renaming rule. The published K1C macros and
virtual SD provide surrounding factory interface references.

The six examined branch/tag trees do not contain the CFS-specific box, serial,
address-manager or box configuration sources. The published generic sensor
handler lacks the target CFS head event. The compared configuration files also
do not match the target copies as complete files. Accordingly, the matrix keeps
factory references, target-binary analysis and configuration snapshots distinct;
an official repository name does not establish the installed CFS Upgrade build.
Source copies, machine-specific values and raw analysis are kept outside the
public catalogue.

The official [K2 CFS integration](https://github.com/CrealityOfficial/K2_Series_Klipper/tree/bc0a52078ace1e4d82995e396b38457b9f441024)
is pinned separately. Its published CFS G32 1.1.3 image matches the examined
baseline byte for byte. Its [head-sensor source](https://github.com/CrealityOfficial/K2_Series_Klipper/blob/bc0a52078ace1e4d82995e396b38457b9f441024/klippy/extras/filament_switch_sensor.py)
also matches the preserved target copy: the custom truthy-state event is sent
after RunoutHelper, including when that helper returns early. The box callback
therefore needs its own operation-context and duplicate-event checks.
`BOX_INFO_REFRESH` also matches the examined target sequence.

K2 has a different native ABI (ARM/CPython 3.9 versus the target MIPS/CPython
3.8), sensor configuration and mechanical workflows. Its native libraries,
additional commands, cutter coordinates and configurations are not adopted as
K1C Upgrade defaults. The matching MCU image does not establish host compatibility.

## Checks available now

Run the existing checkout-based suite:

```sh
python -m unittest discover -s tests -v
```

`test_contract_matrix.py` checks the finite inventories, IDs, stages, real test
references, aliases, conditional registrations, gaps and coverage counts. It
keeps partial and planned cases separate from completed check families.
State/configuration/storage and data-command suites execute synthetic scenarios
and temporary-file operations. Native, protocol, firmware and box-integration
suites cover request bytes, all decoded statuses, payloads, mixed identities and
stale work through substitutes. Lifecycle/error/context tests add cooperative
ready/shutdown, reconnect, heartbeat, reporting and re-entrant head-event cases.
Motion, extrusion and cutting tests exercise configured commands, limits, fan
state, heater and wheel substitutes, sensor latches and calibration failures.
Retained sensor-producer behavior and full loading-result consumption remain
separate from these component checks. The bootstrap check verifies refusal to access
printer objects. None of these tests executes the stock binary or operates equipment.

See [compatibility.md](compatibility.md) for implementation/validation status
and [protocol.md](protocol.md) for the native API boundary.
