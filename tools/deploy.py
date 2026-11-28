"""Install, update or restore Box and restart Klipper on the printer host."""

import argparse
from contextlib import contextmanager
import json
import os
from pathlib import Path
import re
import shutil
import signal
import socket
import stat
import struct
import subprocess
import sys
import tempfile
import time
from urllib.parse import urlsplit
from urllib.request import urlopen

import install


STOCK_SHA256 = "6d7486e1a15cde03f3152eef0d771d0bdd4a90df9b5ef7ecb88bc5d944f20ac4"
EXTENSION_COMMAND = re.compile(r"(?im)^\s*(CFS_PRINT_START_GUARD|CFS_TEST_[A-Z0-9_]+)\b")


def write_json(path, value):
    tmp = path.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as stream:
        json.dump(value, stream, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    tmp.replace(path)


@contextmanager
def deployment_signals(handler):
    previous = {sig: signal.signal(sig, handler) for sig in (signal.SIGINT, signal.SIGTERM)}
    try:
        yield
    finally:
        for sig, restore in previous.items():
            signal.signal(sig, restore)


def interrupt_deployment(signum, _frame):
    raise InterruptedError("deployment interrupted by signal %d" % signum)


def try_write_journal(path, journal):
    try:
        write_json(path, journal)
    except OSError:
        return False
    return True


def prove_stock(data):
    if install.digest(data) != STOCK_SHA256:
        raise ValueError("the original Box entry hash differs from the verified K1C stock wrapper")


def factory_macro_guard(config):
    for name, section in config.items():
        if name.lower().startswith("gcode_macro ") and EXTENSION_COMMAND.search(section.get("gcode", "")):
            raise ValueError("%s calls a CFS extension command. Restore the matching stock-compatible macro before rollback." % name)


class Klipper:
    def __init__(self, pid_file, moonraker):
        self.pid_file = Path(pid_file).resolve()
        self.moonraker = moonraker.rstrip("/")
        address = urlsplit(self.moonraker)
        if (address.scheme != "http" or address.hostname not in ("localhost", "127.0.0.1", "::1")
                or address.path or address.username or address.query or address.fragment):
            raise ValueError("Moonraker must be an HTTP loopback address")
        self.klippy_socket = None
        self.config_file = None

    def get(self, path):
        with urlopen(self.moonraker + path, timeout=3) as response:
            return json.load(response)["result"]

    def ready(self):
        info = self.get("/printer/info")
        if info.get("config_file") != self.config_file:
            raise ValueError("Moonraker is connected to a different Klipper configuration")
        if info["state"] != "ready":
            raise ValueError("Klipper is not ready")
        status = self.get("/printer/objects/query?print_stats&virtual_sdcard&pause_resume&extruder&heater_bed&configfile")["status"]
        if (status["print_stats"]["state"] not in ("standby", "complete", "cancelled")
                or status["virtual_sdcard"]["is_active"] or status["virtual_sdcard"].get("file_path")
                or status["pause_resume"]["is_paused"]
                or status["extruder"]["target"] or status["heater_bed"]["target"]
                or self.get("/server/job_queue/status")["queued_jobs"]):
            raise ValueError("Klipper must be idle, with no paused or queued job and both heaters off")
        return status["configfile"]["config"]

    def process(self, extras):
        pid = int(self.pid_file.read_text(encoding="ascii").strip())
        if pid <= 1:
            raise ValueError("invalid Klipper PID")
        proc = Path("/proc") / str(pid)
        argv = (proc / "cmdline").read_bytes().rstrip(b"\0").split(b"\0")
        entry = os.fsencode(extras.parent / "klippy.py")
        if len(argv) < 3 or argv[1] != entry or not os.path.isabs(argv[0]):
            raise ValueError("PID file does not identify the target Klipper process")
        if b"-a" not in argv or argv.index(b"-a") == len(argv) - 1:
            raise ValueError("Klipper must have an API socket")
        self.klippy_socket = os.fsdecode(argv[argv.index(b"-a") + 1])
        self.config_file = os.fsdecode(argv[2])
        self.check_socket(pid)
        env = dict(part.split(b"=", 1) for part in (proc / "environ").read_bytes().split(b"\0") if part)
        return pid, self.start_time(pid), argv, os.readlink(str(proc / "cwd")), env

    def check_socket(self, expected_pid):
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as peer:
            peer.settimeout(2)
            peer.connect(self.klippy_socket)
            pid, _, _ = struct.unpack("3i", peer.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
            if pid != expected_pid:
                raise ValueError("Klipper API socket is owned by another process")

    @staticmethod
    def start_time(pid):
        try:
            fields = (Path("/proc") / str(pid) / "stat").read_text().rsplit(")", 1)[1].split()
        except FileNotFoundError:
            return None
        return None if fields[0] == "Z" else fields[19]

    def stop(self, pid, started):
        if started is None or self.start_time(pid) != started:
            raise ValueError("Klipper PID changed before stop")
        os.kill(pid, signal.SIGTERM)
        for _ in range(100):
            if self.start_time(pid) != started:
                return
            time.sleep(0.1)
        raise RuntimeError("Klipper did not stop, so Box files were left in place")

    def start(self, argv, cwd, env):
        proc = subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                start_new_session=True)
        try:
            self.pid_file.write_text("%d\n" % proc.pid, encoding="ascii")
        except OSError:
            proc.terminate()
            proc.wait(timeout=10)
            raise
        return proc

    def verify(self, proc, config):
        last = None
        for _ in range(50):
            if proc.poll() is not None:
                raise RuntimeError("Klipper exited after launch")
            try:
                self.check_socket(proc.pid)
                observed = self.ready()
            except (OSError, ValueError, KeyError) as error:
                last = error
                time.sleep(1)
                continue
            if observed != config:
                raise RuntimeError("Klipper loaded different configuration")
            return
        raise RuntimeError("Klipper did not become ready: %s" % last)


def release_files(previous, extras):
    release, backup = (install.safe_path(previous[name]) for name in ("release", "backup"))
    prove_stock(install.safe_path(backup / "box.py.original").read_bytes())
    files = install.release_sources(release)
    manifest = json.loads(install.safe_path(backup / "manifest.json").read_text(encoding="utf-8"))
    if manifest.get("version") != 3 or manifest.get("installed") != {name: install.digest(data) for name, data in files.items()}:
        raise ValueError("the previous installer payload and v3 backup must match")
    install.restore(extras, backup, check_only=True)
    return files, backup


def managed_paths(extras, names):
    """Enumerate exactly the installer-owned paths, including generated caches."""
    paths = [install.safe_path(extras / "box.py")]
    cache_exists, caches = install.box_caches(extras)
    paths.extend(caches)
    cfs = install.safe_path(extras / "cfs")
    dirs = ["__pycache__"] if cache_exists else []
    if cfs.is_dir():
        dirs.append("cfs")
        sources = {Path(name).name for name in names if name.startswith("cfs/")}
        legacy = {Path(name).with_suffix(".pyc").name for name in sources}
        for entry in cfs.iterdir():
            if entry.name == "__pycache__":
                install.safe_path(entry)
                dirs.append("cfs/__pycache__")
                for cached in entry.iterdir():
                    match = re.fullmatch(r"(.+)\.cpython-[0-9]+(?:\.opt-[0-9]+)?\.pyc", cached.name)
                    if not match or match[1] + ".py" not in sources:
                        raise ValueError("unexpected managed cache entry: " + cached.name)
                    paths.append(install.safe_path(cached))
            elif entry.name in sources or entry.name in legacy:
                paths.append(install.safe_path(entry))
            else:
                raise ValueError("unexpected file in cfs: " + entry.name)
    elif cfs.exists():
        raise ValueError("cfs is not a directory")
    if not all(path.is_file() for path in paths):
        raise ValueError("managed files must be regular files")
    return dirs, paths


def snapshot(extras, names, target):
    dirs, paths = managed_paths(extras, names)
    for path in paths:
        saved = target / path.relative_to(extras)
        saved.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, saved)
    return dirs, [str(path.relative_to(extras)) for path in paths]


def reinstate(extras, names, target, saved):
    dirs, files = saved
    current_dirs, current_files = managed_paths(extras, names)
    for path in current_files:
        if str(path.relative_to(extras)) not in files:
            path.unlink()
    for name in dirs:
        (extras / name).mkdir(exist_ok=True)
    for name in files:
        shutil.copy2(target / name, extras / name)
    for name in reversed(current_dirs):
        if name not in dirs and not any((extras / name).iterdir()):
            (extras / name).rmdir()


def stage_release(release, files):
    for name, data in files.items():
        path = release / ("klipper/box.py" if name == "box.py" else "src/" + name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    (release / "tools").mkdir()
    shutil.copy2(install.__file__, release / "tools" / "install.py")


def deploy(args, host, extras, state):
    current_file, pending_file = state / "current.json", state / "pending.json"
    if pending_file.exists():
        raise ValueError("unfinished deployment. Review " + str(pending_file))
    original_current = json.loads(current_file.read_text(encoding="utf-8")) if current_file.exists() else None
    current = original_current
    provided = bool(args.previous_release), bool(args.previous_backup)
    if provided[0] != provided[1] or provided[0] and (current or args.action == "install"):
        raise ValueError("supply both previous paths only when adopting an unmanaged extension")
    if provided[0]:
        current = {"release": str(install.safe_path(args.previous_release)),
                   "backup": str(install.safe_path(args.previous_backup))}
    if (args.action == "install") != (current is None):
        raise ValueError("choose install for stock or update/rollback for an existing deployment")
    launch = host.process(extras)
    config = host.ready()
    if args.action == "rollback":
        factory_macro_guard(config)
    new_files = install.sources() if args.action != "rollback" else {}
    if current:
        old_files, old_backup = release_files(current, extras)
    else:
        old_files, old_backup = {}, None
        prove_stock(install.safe_path(extras / "box.py").read_bytes())
        install.prepare(extras, state / "preview-backup")
    names = set(old_files) | set(new_files)
    if args.check:
        managed_paths(extras, names)
        return
    transaction = Path(tempfile.mkdtemp(prefix="deployment-", dir=str(state)))
    release, backup, previous = (transaction / name for name in ("release", "factory", "previous"))
    if new_files:
        stage_release(release, new_files)
    recorded = snapshot(extras, names, previous)
    journal = {"action": args.action, "phase": "stop", "transaction": str(transaction), "current": current,
               "extras": str(extras), "snapshot": recorded, "old_pid": launch[0]}
    if host.ready() != config or host.process(extras) != launch:
        raise ValueError("printer state changed during preflight")
    write_json(pending_file, journal)
    started = None
    started_time = None
    stopped = False
    try:
        host.stop(*launch[:2])
        stopped = True
        journal["phase"] = "replace"
        write_json(pending_file, journal)
        if old_backup:
            install.restore(extras, old_backup)
        if new_files:
            install.install(extras, backup, files=new_files)
        journal["phase"] = "activate"
        write_json(pending_file, journal)
        interrupts = []
        # Keep the spawned identity available for rollback if a signal arrives
        # during launch or PID-file publication.
        with deployment_signals(lambda signum, _frame: interrupts.append(signum)):
            started = host.start(*launch[2:])
            started_time = host.start_time(started.pid)
        if interrupts:
            interrupt_deployment(interrupts[0], None)
        host.verify(started, config)
        if new_files:
            install.restore(extras, backup, check_only=True)
            write_json(current_file, {"release": str(release), "backup": str(backup)})
        else:
            if ((extras / "box.py").read_bytes() != (old_backup / "box.py.original").read_bytes()
                    or (extras / "cfs").exists()):
                raise RuntimeError("activated original Box files failed verification")
            if current_file.exists():
                current_file.unlink()
    except (Exception, KeyboardInterrupt) as error:
        if not stopped:
            journal.update(phase="stop_uncertain", error=str(error))
            try_write_journal(pending_file, journal)
            raise RuntimeError("Klipper stop was not confirmed (%s). Inspect %s before retrying."
                               % (error, pending_file)) from error
        with deployment_signals(signal.SIG_IGN):
            journal.update(phase="reinstate", error=str(error))
            try_write_journal(pending_file, journal)
            try:
                if started is not None and started.poll() is None:
                    host.stop(started.pid, started_time)
                reinstate(extras, names, previous, recorded)
                host.verify(host.start(*launch[2:]), config)
            except Exception as rollback_error:
                raise RuntimeError("deployment failed (%s); previous launch failed (%s). See %s"
                                   % (error, rollback_error, pending_file)) from rollback_error
            journal["phase"] = "previous_ready"
            try:
                pointer = json.loads(current_file.read_text(encoding="utf-8")) if current_file.exists() else None
                if pointer != original_current:
                    if original_current is None:
                        current_file.unlink()
                    else:
                        write_json(current_file, original_current)
                if not try_write_journal(transaction / "failed.json", journal):
                    raise OSError("could not record the completed recovery")
                pending_file.unlink()
            except (OSError, ValueError) as journal_error:
                raise RuntimeError("previous deployment is ready; journal needs inspection at %s (%s)"
                                   % (pending_file, journal_error)) from error
        raise RuntimeError("previous deployment restored and ready after failure: %s" % error) from error
    pending_file.replace(transaction / "completed.json")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("install", "update", "rollback"))
    parser.add_argument("--extras", required=True)
    parser.add_argument("--state-dir", required=True)
    parser.add_argument("--pid-file", default="/var/run/klippy.pid")
    parser.add_argument("--moonraker", default="http://127.0.0.1:7125")
    parser.add_argument("--check", action="store_true", help="check prerequisites without stopping Klipper or changing Box files")
    parser.add_argument("--previous-release")
    parser.add_argument("--previous-backup")
    args = parser.parse_args()
    try:
        if not sys.platform.startswith("linux"):
            raise ValueError("deploy must run on the Linux Klipper host")
        import fcntl
        extras, state = install.safe_path(args.extras), install.safe_path(args.state_dir)
        if not extras.is_dir() or not state.parent.is_dir() or extras == state or extras in state.parents or state in extras.parents:
            raise ValueError("state directory must be separate from extras")
        state.mkdir(mode=0o700, exist_ok=True)
        info = state.stat()
        if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) & 0o022:
            raise ValueError("state directory must belong to the current user and disallow writes by others")
        with install.safe_path(state / "lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with deployment_signals(interrupt_deployment):
                deploy(args, Klipper(args.pid_file, args.moonraker), extras, state)
    except (OSError, ValueError, RuntimeError, KeyError, TypeError) as error:
        print("Deployment error:", error, file=sys.stderr)
        return 1
    print("Box %s %s. Klipper is ready." % (args.action, "preflight passed" if args.check else "complete"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
