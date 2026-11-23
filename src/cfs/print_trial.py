"""Bound one reviewed virtual-SD job while leaving normal startup passive."""

from contextlib import contextmanager
from copy import deepcopy
from dataclasses import replace
import hashlib
from math import isfinite
from pathlib import Path
import re
import struct

from .native import UnsupportedRequest, build_request, slot_mask


class PrintTrial:
    """Own a file, actual worker and finite callback scopes, not global mechanics.

    The caller must stop future virtual-SD commands after a revoked lease. A
    cooperative deadline cannot interrupt native waits or already queued motion.
    """

    TIMEOUT = 1800
    MAX_FILE_BYTES = 2 * 1024 * 1024
    MAX_TOOL_CALLS = 8
    MAX_REQUESTS = 2048
    COMMANDS = frozenset((
        "BOX_START_PRINT", "BOX_START_PRINT_EXTRUDE_MATERIAL", "BOX_END",
        "BOX_END_PRINT", "SET_BOX_MODE_PRINT_WHEN_NOZZLE_CLEAR",
        "BOX_CHECK_MATERIAL_REFILL", "BOX_RESUME_EXTRUDE",
        "BOX_TNN_RETRY_PROCESS", "BOX_ERROR_CLEAR", "BOX_ADD_TNN",
        "BOX_EXTRUSION_ALL_MATERIALS", "WAIT_EXTRUSION_ALL_MATERIALS",
    ))

    def __init__(self, box):
        self.box = box
        self._token = None
        self._busy = 0
        self._owners = {}
        self._pending_commands = {}
        self._commands = {}
        self._requests = {}
        self._responses = set()
        self._request_ids = {}
        self._started = self._ending = self._end_done = self._runout = False
        self._phase = None
        self._unknown_source = False
        self._starting_observations = False

    @property
    def active(self):
        return self._token is not None

    def cancel(self):
        """Revoke future effects without inventing cleanup or moving equipment."""
        self._token = None
        self._pending_commands.clear()

    def _config(self):
        values = {key: value for key, value in vars(self.box.boxcfg).items() if key != "config"}
        config = self.box._printer.lookup_object("configfile").get_status(self.box._reactor.monotonic())
        return deepcopy((values, config["config"], config["settings"]))

    def _maps(self):
        return deepcopy((self.box.box_state.Tn_map, self.box.box_state.Tnn_map))

    def _materials(self):
        state = self.box.box_state
        return tuple((tool, state.get_Tn_data(self.address, "material_type", tool[2]),
                      state.get_Tn_data(self.address, "color_value", tool[2])) for tool in self.tools)

    def _file(self):
        before = self._file_stat()
        if not 0 < before[2] <= self.MAX_FILE_BYTES:
            raise ValueError("the reviewed print file exceeds its size bound")
        data = self.path.read_bytes()
        if (not data or len(data) > self.MAX_FILE_BYTES or hashlib.sha256(data).hexdigest() != self.sha256
                or before != self._file_stat()):
            raise RuntimeError("the approved print file changed")
        self._file_stamp = before
        return data

    def _file_stat(self):
        status = self.path.stat()
        return (status.st_dev, status.st_ino, status.st_size, status.st_mtime_ns, status.st_ctime_ns)

    def arm(self, filename, sha256, tools, confirmed=False):
        """Review immutable inputs only; loading the job remains a separate action."""
        box, lifecycle = self.box, self.box.lifecycle
        if confirmed is not True or self.active or self._busy:
            raise RuntimeError("one print trial needs explicit confirmation and no previous owner")
        if lifecycle is None or not lifecycle.passive:
            raise RuntimeError("the print trial requires passive startup")
        lifecycle.check()
        if (box.print_stats.state != "standby" or box.operations.active
                or box.material_trial.active or box.preload_trial.active or box.error is not None):
            raise RuntimeError("the host must be idle before arming a print")
        if box.box_state.get_Tn_data(0, uppart="enable") != 1:
            raise RuntimeError("CFS must remain enabled for the reviewed print")
        self._stage7_amount()
        if (not isinstance(filename, str) or not filename or ".." in Path(filename).parts
                or not isinstance(sha256, str) or re.fullmatch(r"[0-9a-f]{64}", sha256) is None):
            raise ValueError("a print needs a reviewed filename and SHA-256")
        if (not isinstance(tools, (tuple, list)) or len(tools) != 2
                or any(not isinstance(tool, str) for tool in tools) or len(set(tools)) != 2):
            raise ValueError("the reference print needs two distinct physical slots")
        if any(tool not in box.box_state.Tnn_content for tool in tools):
            raise ValueError("the reference print needs actual physical slots")
        addresses = {box.box_state.get_Tnn_content(tool)["addr"] for tool in tools}
        if len(addresses) != 1:
            raise ValueError("the reference print permits one selected CFS")
        address = addresses.pop()
        sd = box.virtual_sdcard
        root = Path(sd.sdcard_dirname).resolve()
        candidate = Path(filename)
        if not candidate.is_absolute():
            candidate = root / candidate
        if candidate.is_symlink():
            raise ValueError("a print file must not be a symbolic link")
        path = candidate.resolve()
        if root not in path.parents or path.suffix.lower() not in (".gcode", ".g", ".gco") or not path.is_file():
            raise ValueError("the print must be a regular G-code file under virtual SD")
        if getattr(sd, "current_file", None) is not None:
            raise RuntimeError("another virtual-SD file is already open")
        if (box.boxcfg.version != 1 or box.device_versions.require_supported(address).version != "1.1.3"
                or not lifecycle.online(address) or not box.native.is_connected
                or any(lifecycle.online(i) for i in range(1, 5) if i != address)):
            raise RuntimeError("the reference job requires one connected cfs0 MCU 1.1.3")
        self.path, self.sha256, self.tools, self.address = path, sha256, tuple(tools), address
        data = self._file()
        lines = [line.strip().split(";", 1)[0].strip() for line in data.decode("utf-8").splitlines()]
        commands = [line.split()[0].upper() for line in lines if line]
        sequence = [line for line in lines if re.fullmatch(r"T\d+", line)]
        if (commands.count("START_PRINT") != 1 or commands.count("END_PRINT") != 1
                or not sequence or len(sequence) > self.MAX_TOOL_CALLS - 1):
            raise ValueError("the bounded job needs one start/end and a finite logical tool sequence")
        for logical in sequence:
            if logical not in box.box_state.Tn_map or box.box_state.resolve_tool(logical) not in self.tools:
                raise ValueError("a job tool is outside the approved physical pair")
        if {box.box_state.resolve_tool(logical) for logical in sequence} != set(self.tools):
            raise ValueError("the reviewed job must use both approved physical slots")
        self.sequence = tuple(sequence)
        self._logical = frozenset(sequence)
        self.source, self.target = None, box.box_state.resolve_tool(sequence[0])
        self._lifecycle, self._native = lifecycle, box.native
        self._generation, self._binding = lifecycle._generation, box._binding_generation
        self._device = box.device_versions.generation(address)
        self._identity = deepcopy(box.device_versions.get(address))
        self._configuration, self._mapping = self._config(), self._maps()
        self._material = self._materials()
        self._deadline = box._reactor.monotonic() + self.TIMEOUT
        self._requests.clear()
        self._responses.clear()
        self._request_ids.clear()
        self._tool_calls = 0
        self._started = self._ending = self._end_done = self._runout = False
        self._token = object()
        return {"filename": str(path), "sha256": sha256, "tools": self.tools,
                "sequence": self.sequence, "mechanics": False}

    def _job(self):
        sd = self.box.virtual_sdcard
        current = getattr(sd, "current_file", None)
        if current is None or Path(current.name).resolve() != self.path or current.closed:
            raise RuntimeError("the current virtual-SD file is not the approved job")
        if deepcopy(sd.gcode_metadata) != self._metadata:
            raise RuntimeError("the approved print metadata changed")

    def _check(self, token, unstarted=False):
        try:
            box, lifecycle = self.box, self.box.lifecycle
            if token is None or token is not self._token or lifecycle is not self._lifecycle:
                raise RuntimeError("the print trial was revoked or replaced")
            lifecycle.check(self._generation)
            if (not lifecycle.passive or box.native is not self._native
                    or box._binding_generation != self._binding
                    or box.device_versions.generation(self.address) != self._device
                    or box.device_versions.get(self.address) != self._identity
                    or not box.native.is_connected or not lifecycle.online(self.address)
                    or any(lifecycle.online(i) for i in range(1, 5) if i != self.address)
                    or box._reactor.monotonic() >= self._deadline
                    or self._config() != self._configuration
                    or self._materials() != self._material
                    or box.box_state.get_Tn_data(0, uppart="enable") != 1
                    or (box.error is not None and not (self._runout and box.error == "filament_err"))):
                raise RuntimeError("the print's device, configuration, error or deadline changed")
            maps = self._maps()
            identity_map = {name: name for name in self._mapping[1]}
            if maps != self._mapping and not (self._ending and maps == (self._mapping[0], identity_map)):
                raise RuntimeError("the approved print mapping changed")
            # Hash at arming and actual start; frequent cooperative guards pin
            # the inode/size/write metadata without rereading the entire job.
            if self._file_stat() != self._file_stamp:
                raise RuntimeError("the approved print file changed")
            if self._started:
                if box.print_stats.state not in ("printing", "paused"):
                    raise RuntimeError("the print is no longer running or paused")
                self._job()
            elif not unstarted:
                raise RuntimeError("the print start guard has not claimed the job")
        except Exception:
            # An old scheduled child must not revoke a newer lease.
            if token is self._token:
                self.cancel()
            raise

    def _worker(self):
        sd = self.box.virtual_sdcard
        current = self._lifecycle.current_dispatch()
        return (current is not None and current is getattr(sd, "work_handler_dispatch", None)
                and getattr(sd, "cmd_from_sd", False) is True)

    def owns_dispatch(self):
        if not self.active or not self._started or self.box.lifecycle is not self._lifecycle:
            return False
        current = self._lifecycle.current_dispatch()
        return self._worker() or any(token is self._token for token, _ in self._owners.get(current, ()))

    def guard(self, address=None):
        offline = (type(address) is int and address in range(1, 5) and address != self.address
                   and not self._lifecycle.online(address)
                   and self.box.box_state.get_Tn_data(address, "state") != "connect") if self.active else False
        if not self.owns_dispatch() or (address not in (None, self.address) and not offline):
            raise UnsupportedRequest("this dispatch does not own the selected print CFS")
        token = self._token

        def check():
            self._check(token)
            if offline and (self._lifecycle.online(address)
                    or self.box.box_state.get_Tn_data(address, "state") == "connect"):
                self.cancel()
                raise RuntimeError("an offline-only print guard became connected")
            if not self.owns_dispatch():
                raise RuntimeError("the print dispatch left its finite scope")
        check()
        return check

    def context_guard(self):
        return self.guard() if self.owns_dispatch() else None

    def start_guard(self, gcmd):
        token = self._token
        self._check(token, unstarted=True)
        if self._started or not self._worker() or self.box.print_stats.state != "printing":
            raise RuntimeError("only the approved virtual-SD worker may start the print")
        sd = self.box.virtual_sdcard
        self._metadata = deepcopy(sd.gcode_metadata)
        if not isinstance(self._metadata, dict):
            self.cancel()
            raise RuntimeError("the current print has no captured metadata")
        try:
            self._file()
            self._job()
            first = sd.get_first_Tn(None)
            explicit = gcmd.get_int("INITIAL_TOOL", -1)
            if (first != self.sequence[0] or first not in self._logical
                    or (explicit != -1 and (not 0 <= explicit <= 15 or first != "T%d" % explicit))):
                raise ValueError("the explicit or file-derived initial tool does not match the approved job")
        except Exception:
            self.cancel()
            raise
        self._started = True
        self._starting_observations = True
        try:
            mode = self._fresh("GET_BOX_STATE")[3]
            material = self._fresh("GET_FILAMENT_SENSOR_STATE", b"\x00")[0]
            connections = self._fresh("GET_FILAMENT_SENSOR_STATE", b"\x01")[0]
            self._fresh("GET_BUFFER_STATE")
            head = self._lifecycle.head_sensor.runout_helper.filament_present
            source = self.box.last_cmd
            live = self.box.box_state.get_Tn_data(self.address, "filament")
            mask = sum(slot_mask(tool[2]) for tool in self.tools)
            absent = source is None and live in (None, "None")
            known = source in self.tools and live == source
            if (mode not in (0, 2) or material & mask != mask
                    or type(head) not in (bool, int) or head not in (0, 1)
                    or not ((absent and head == 0 and connections == 0)
                            or (known and head == 1 and connections == slot_mask(source[2])))):
                raise RuntimeError("fresh print head/source and CFS observations do not match")
            self.guard()
        except Exception:
            self.cancel()
            raise
        finally:
            self._starting_observations = False
        return self.box.box_state.resolve_tool(first)

    def _fresh(self, command, data=b""):
        """Require this selected read's normal frame, without source substitution."""
        previous = self.box.last_replies.get(self.address)
        request = build_request(self.address, command, data)
        result = self.box.send_native(request, retries_en=False)
        frame = self.box.last_replies.get(self.address)
        if (result is not True or frame is None or frame is previous or frame.status != 0
                or frame.address != self.address or frame.command != request.command):
            raise RuntimeError("the print start needs its own fresh normal observations")
        self.guard()
        return frame.data

    @contextmanager
    def execution(self, kind="callback", token=None):
        """Enter a finite trusted callback/control scope for one captured lease."""
        token = self._token if token is None else token
        self._check(token)
        if kind not in ("callback", "head", "control", "protocol"):
            raise ValueError("unknown print execution scope")
        if kind == "head" and (not self.box.operations.active or not self.box.filament_sensor_enable
                or self.box.operations.address != self.address or self.box.operations.target not in self.tools
                or self.box.extrude_tnn != self.box.operations.target
                or self.box.extrude_process_stage7_flag
                or self._lifecycle.head_sensor.runout_helper.filament_present != 1):
            raise RuntimeError("there is no armed real head arrival")
        current = self._lifecycle.current_dispatch()
        if current is None:
            raise RuntimeError("a print callback needs an actual dispatch")
        entries = self._owners.setdefault(current, [])
        entries.append((token, kind))
        self._busy += 1
        try:
            yield
            self._check(token)
        except Exception:
            if token is self._token:
                self.cancel()
            raise
        finally:
            entries.pop()
            if not entries:
                self._owners.pop(current, None)
            self._busy -= 1

    def capture(self, kind="callback"):
        """Return a zero-argument context factory for already-owned scheduled work."""
        if not self.owns_dispatch():
            return None
        token = self._token
        self.guard()
        return lambda: self.execution(kind, token)

    def change_phase(self, name, function, *args, **kwargs):
        """Scope existing tool-change phases without replacing their mechanics."""
        self.guard()
        if name not in ("UNLOAD", "LOAD", "FLUSH") or self._phase is not None:
            raise RuntimeError("the print tool-change phase is invalid or nested")
        self._phase = name
        self._phase_source = self.box.last_cmd
        self._unknown_source = self._phase_source is None
        serial = getattr(self.box.operations, "_serial", None)
        try:
            if name == "FLUSH" and self._change_flush_profile() != self._change_purge:
                raise RuntimeError("the print purge changed after preflight")
            result = function(*args, **kwargs)
            self.guard()
            if result is not True:
                raise RuntimeError("the print tool-change phase did not complete")
            self._phase_observations(name, serial)
            return result
        except Exception:
            self.cancel()
            raise
        finally:
            self._phase = None

    def _phase_observations(self, name, serial=None):
        """Keep physical phase completion separate from command ACKs and hints."""
        mode = self._fresh("GET_BOX_STATE")[3]
        material = self._fresh("GET_FILAMENT_SENSOR_STATE", b"\x00")[0]
        connections = self._fresh("GET_FILAMENT_SENSOR_STATE", b"\x01")[0]
        buffer = self._fresh("GET_BUFFER_STATE")[0]
        head = self._lifecycle.head_sensor.runout_helper.filament_present
        if type(head) not in (bool, int) or head not in (0, 1):
            raise RuntimeError("the print phase has no actual head observation")
        source, live = self.box.last_cmd, self.box.box_state.get_Tn_data(self.address, "filament")
        if name == "UNLOAD":
            box = self.box
            # The retained drain clears the source but keeps its old live label.
            # Only a decoded runout and freshly empty path can explain that hint.
            drained_label = (self._runout and self._unknown_source
                and self.source in self.tools and live == self.source == box.last_cmd_tmp
                and box.error is None and box.is_use_ending_material is False
                and box.box_filament_err is False
                and box.box_state.get_Tn_data(self.address, "vender", self.source[2]) == "none")
            empty_label = (live in (None, "None")
                and mode in ((0, 2) if self._unknown_source else (0,))
                and buffer in ((0, 2) if self._unknown_source else (2,)))
            valid = (head == 0 and connections == 0 and source is None
                and (empty_label or (drained_label and mode == 0 and buffer == 2)))
        else:
            valid = (self.target in self.tools and head == 1 and source == live == self.target
                and mode == 2 and connections == slot_mask(self.target[2])
                and material & slot_mask(self.target[2])
                and buffer in ((0, 1, 2) if name == "FLUSH" else (0, 2)))
        if not valid:
            raise RuntimeError("fresh CFS/head/source observations did not complete the print phase")
        if name == "UNLOAD" and drained_label and mode == 0 and buffer == 2:
            operation = self.box.operations
            if (serial is None or serial != operation._serial or not operation.active
                    or (operation.source, operation.target, operation.address)
                        != (self.source, self.target, self.address)):
                raise RuntimeError("the drained source lost its owning operation")
            if operation._error == "filament_err":
                operation.acknowledge_error_clear(serial, self.source, self.target, self.address)
            elif operation._error is not None:
                raise RuntimeError("the drained source has a different captured error")

    def _change_flush_profile(self):
        # Reuse the bounded stock calculation rather than introducing a second
        # colour/metadata policy.
        from .operations import bounded_change_flush
        return bounded_change_flush(self.box, self.source, self.target)

    def prepare_change(self, logical):
        """Bind every actual coordinator entry, including deferred/retry children.

        A loaded same-source selection has no colour purge. Its continuation
        branch uses the configured extrusion directly, rather than the slicer
        matrix's zero diagonal. This check precedes metadata and cut effects.
        """
        self.guard()
        try:
            if self._end_done or self._phase is not None or self._tool_calls >= self.MAX_TOOL_CALLS:
                raise RuntimeError("the print cannot start another tool coordinator")
            target = self.box.box_state.resolve_tool(logical)
            source = self.box.last_cmd
            head = self._lifecycle.head_sensor.runout_helper.filament_present
            if (target not in self.tools or (source is not None and source not in self.tools)
                    or type(head) not in (bool, int) or head not in (0, 1)):
                raise RuntimeError("the tool coordinator has an unapproved source, target or head observation")
            self._tool_calls += 1
            self.source, self.target, self._guard = source, target, self.guard()
            self._change_purge = None
            same_loaded = source == target and head == 1 and self.box.error in (None, "filament_err")
            if same_loaded:
                if self.box.box_state.is_continue_print:
                    length = self.box.boxcfg.Tn_extrude
                    if type(length) not in (int, float) or not isfinite(length) or not 0 < length <= 2000:
                        raise ValueError("same-source continuation needs bounded positive configured extrusion")
            else:
                self._change_purge = self._change_flush_profile()
            self.guard()
            return target
        except Exception:
            self.cancel()
            raise

    def owns_phase(self, action, name):
        if not self.owns_dispatch() or self._phase != action or name != "BOX_RETRUDE_MATERIAL":
            return False
        self.guard()
        if (not self._unknown_source and (self.box.last_cmd != self._phase_source
                or self.box.box_state.get_Tn_data(self.address, "filament") != self._phase_source)):
            self.cancel()
            raise RuntimeError("the print unload lost its known source")
        return True

    def _bounded_profile(self, hot=False):
        self.guard()
        box = self.box
        status = box.gcode_move.get_status(box._reactor.monotonic())
        if (not callable(getattr(box.toolhead, "flush_step_generation", None))
                or any(type(status[key]) not in (int, float) or status[key] != 1
                       for key in ("speed_factor", "extrude_factor"))):
            raise ValueError("the print withdrawal needs actual unit scales and step submission")
        if hot:
            heater = box.toolhead.extruder.get_heater()
            actual, _ = heater.get_temp(box._reactor.monotonic())
            if (not self.owns_phase("UNLOAD", "BOX_RETRUDE_MATERIAL") or self._unknown_source
                    or heater.can_extrude is not True or type(actual) not in (int, float)
                    or not isfinite(actual) or not heater.min_extrude_temp <= actual < heater.max_temp):
                raise RuntimeError("the print withdrawal requires a hot known source")
        self.guard()

    def control(self, name, action, *args, **kwargs):
        if name not in ("PAUSE", "RESUME", "CANCEL_PRINT", "BOX_TNN_RETRY_PROCESS", "BOX_ERROR_CLEAR"):
            raise UnsupportedRequest("this is not a permitted print control boundary")
        if name in ("RESUME", "BOX_TNN_RETRY_PROCESS") and self.box.print_stats.state != "paused":
            raise RuntimeError("resume/retry requires the actual paused job")
        try:
            with self.execution("control"):
                return action(*args, **kwargs)
        finally:
            if name == "CANCEL_PRINT":
                self.cancel()

    def allows_command(self, name, gcmd):
        if not self.owns_dispatch() or self._end_done:
            return False
        params = gcmd.get_command_parameters()
        parameterless = not params
        if re.fullmatch(r"T[0-9]+", name) and params == {"T": name[1:]}:
            # The retained traditional parser includes the command token in
            # its parameter dictionary; it is not an extra tool argument.
            raw = getattr(gcmd, "get_raw_command_parameters", None)
            parameterless = callable(raw) and not raw().split(";", 1)[0].strip()
        deferred = (self._worker()
                    and name == getattr(self.box.virtual_sdcard, "resume_tnn", None)
                    and name in self.box.box_state.Tnn_map
                    and self.box.box_state.resolve_tool(name) in self.tools)
        if name in self._logical or deferred:
            allowed = parameterless and self._tool_calls < self.MAX_TOOL_CALLS
        else:
            allowed = name in self.COMMANDS
        if not allowed:
            return False
        self.guard()
        self._pending_commands[self._lifecycle.current_dispatch()] = (name, gcmd, self._token)
        return True

    def call_command(self, action, gcmd):
        current = self._lifecycle.current_dispatch()
        pending = self._pending_commands.pop(current, None)
        if pending is None or pending[1] is not gcmd or pending[2] is not self._token:
            raise UnsupportedRequest("the print command has no exact admitted owner")
        name, _, token = pending
        if name == "BOX_END_PRINT":
            self._ending = True
        commands = self._commands.setdefault(current, [])
        commands.append(name)
        self._busy += 1
        try:
            self.guard()
            result = action(gcmd)
            self.guard()
            if result is False:
                raise RuntimeError("an owned print command did not complete")
            if name == "BOX_END_PRINT":
                self._end_done = True
            return result
        except Exception:
            if token is self._token:
                self.cancel()
            raise
        finally:
            commands.pop()
            if not commands:
                self._commands.pop(current, None)
            self._busy -= 1

    def admit(self, request):
        if not self.owns_dispatch():
            return None
        if request.address != self.address:
            self.cancel()
            raise UnsupportedRequest("the print cannot send to another CFS")
        guard = self.guard(request.address)
        data, command = request.data, request.command
        if self._end_done and command not in (5, 8, 10, 14):
            self.cancel()
            raise UnsupportedRequest("the completed print cannot start another native action")
        masks = tuple(slot_mask(tool[2]) for tool in self.tools)
        scopes = self._commands.get(self._lifecycle.current_dispatch(), ())
        starting_ending = any(name in ("BOX_START_PRINT", "BOX_END_PRINT") for name in scopes)
        allowed = ((command in (5, 10) and data == b"")
            or (command in (8, 14) and data in (b"\x00", b"\x01"))
            or (command == 4 and data in (b"\x00\x01",) + tuple(bytes((mask, 0)) for mask in masks))
            or (command == 13 and starting_ending and data in (b"\x0f\x00", b"\x0f\x01"))
            or (command == 15 and (starting_ending or self._runout) and data in (b"\x00", b"\x01"))
            or (command == 16 and len(data) == 3 and data[0] in masks
                and data[1] in (0, 4, 5, 6, 7)
                and data[2] == (self._stage7_amount() if data[1] == 7 else 0))
            or (command == 17 and len(data) == 2 and data[0] in (0,) + masks and data[1] in (0, 1)))
        if (request.diagnostic or request.request_state != 0xFF or not allowed
                or len(self._requests) >= self.MAX_REQUESTS or id(request) in self._requests):
            self.cancel()
            raise UnsupportedRequest("request is outside the bounded print contract")
        if command == 16 and data[1] == 6:
            current = self._lifecycle.current_dispatch()
            if not any(kind == "head" for token, kind in self._owners.get(current, ()) if token is self._token):
                self.cancel()
                raise UnsupportedRequest("stage6 requires the print's armed head callback")
        self._requests[id(request)] = request
        self._request_ids[id(request)] = id(request)
        guard()
        return guard

    def _stage7_amount(self):
        amount = self.box.boxcfg.extrude_material_len_for_box
        if (type(amount) not in (int, float) or not isfinite(amount)
                or not 0 <= amount <= 255 or amount != int(amount)):
            raise ValueError("the print stage7 amount must be an exact configured byte")
        return int(amount)

    def bounded_request(self, request):
        self.guard(request.address)
        if self._requests.get(id(request)) is not request:
            raise UnsupportedRequest("the print request was not admitted")
        remaining = self._deadline - self.box._reactor.monotonic()
        cap = 2 if request.command in (5, 8, 10, 14) else 150
        bounded = replace(request, timeout=min(request.timeout, remaining, cap))
        self._requests[id(bounded)] = bounded
        self._request_ids[id(bounded)] = self._request_ids[id(request)]
        return bounded

    def validate_reply(self, request, frame):
        self.guard(request.address)
        key = self._request_ids.get(id(request))
        if self._requests.get(id(request)) is not request or key in self._responses:
            self.cancel()
            raise UnsupportedRequest("the print reply does not own a pending request")
        if frame is None or frame.address != request.address or frame.command != request.command:
            self.cancel()
            raise RuntimeError("the print needs its own fresh native reply")
        self._responses.add(key)
        if (request.command == 10 and frame.status == 0x50 and len(frame.data) == 4
                and not self._starting_observations):
            self._runout = True
            return  # A genuine runout remains a nonnormal frame for the decoder.
        if frame.status != 0:
            self.cancel()
            raise RuntimeError("an abnormal print reply stopped the trial")
        data, command = frame.data, request.command
        valid = ((command == 10 and len(data) == 4 and data[3] in (0, 1, 2))
            or (command in (4, 13, 15, 17) and data == b"")
            or (command == 5 and len(data) == 1 and data[0] <= 2)
            or (command == 8 and len(data) == 1 and data[0] <= 15)
            or (command == 14 and len(data) == 4 and isfinite(struct.unpack(">f", data)[0]))
            or (command == 16 and ((request.data[1] == 0 and data == b"\x00")
                or (request.data[1] == 5 and len(data) == 4 and isfinite(struct.unpack(">f", data)[0]))
                or (request.data[1] in (4, 6, 7) and data == b""))))
        if not valid:
            self.cancel()
            raise ValueError("unexpected print reply data")

    def capture_protocol(self, request):
        """Capture before exchange; only actual selected runout decoding gets scope."""
        if (not self.active or not self._started or request.address != self.address or request.command != 10
                or request.diagnostic or request.request_state != 0xFF or request.data != b""):
            return None
        token = self._token

        @contextmanager
        def scope(frame):
            self._check(token)
            if (frame is not None and frame.address == self.address and frame.command == 10
                    and frame.status == 0x50 and len(frame.data) == 4):
                self._runout = True
                with self.execution("protocol", token):
                    yield
            else:
                yield
        return scope

    def poll(self):
        """Observe terminal state/deadline; return whether the same lease is live."""
        if not self.active:
            return False
        try:
            self._check(self._token, unstarted=True)
        except Exception:
            return False
        return True
