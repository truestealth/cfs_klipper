#!/usr/bin/env python3
"""Plan or delegate firmware changes for a single isolated cfs0 G32."""

import argparse
from contextlib import contextmanager
import hashlib
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
from tempfile import TemporaryDirectory


BACKEND_SHA256 = "d8f03fbe0fb127dd5db2fb2323680c713cd39abf22b8ad06b72633e56d409d5c"
IMAGE_SHA256 = {
    "113": "386a1106391a332e6a97803c5ce00b87b6d3ddc4c3d61f6fe07fb19fd3125b76",
    "122": "b20a0b5420cf7cfabf5c2938e1067674b6ceccd7a2d9cf1a2dd3d1a184143e2b",
    "142": "e40c9971b2d487b6710c940ac831545f92fafc5b88a22fe4c7d9e35f0e086439",
    "150": "bde552ef5056037989457e294f88cfa7590b4c9251cda5e40adf0a4fae46c5a3",
    "152": "ed969a32b6f55faed0d41f42d94be6d4fc9ca1598208471dc6179dc0a75435af",
    "153": "5b076563f474da1e8741ee88a9b019c2dcb1b7de6c1c651f201f1be61c345dea",
}
BOOT = "cfs0_050_G32"
APP = "cfs0_000_"


def checked_bytes(path, expected):
    if not path.is_file():
        raise ValueError("expected a regular file: %s" % path)
    data = path.read_bytes()
    if hashlib.sha256(data).hexdigest() != expected:
        raise ValueError("SHA-256 mismatch for %s" % path.name)
    return data


def image(path):
    match = re.fullmatch(BOOT + "-" + APP + r"(\d{3})\.bin", path.name)
    if not match or match[1] not in IMAGE_SHA256:
        raise ValueError("not a known cfs0 G32 application image: %s" % path.name)
    version = match[1]
    return version, checked_bytes(path, IMAGE_SHA256[version])


def version_text(version):
    return ".".join(version)


def check_bus(device):
    """Require access to inspect local serial owners before any backend call."""
    if sys.platform != "linux" or os.geteuid() != 0:
        raise ValueError("hardware commands require Linux root to inspect serial owners")
    if not device.is_absolute() or not stat.S_ISCHR(device.stat().st_mode):
        raise ValueError("use the absolute path of the CH340 USB-RS485 device")
    tty = (Path("/sys/class/tty") / device.name / "device").resolve(strict=True)
    if not any((directory / "idVendor").is_file()
               and (directory / "idVendor").read_text().strip().lower() == "1a86"
               for directory in (tty, *tty.parents)):
        raise ValueError("serial device is not a CH340/1a86 USB adapter")
    device_id = device.stat().st_rdev
    for process in Path("/proc").iterdir():
        if not process.name.isdecimal():
            continue
        try:
            arguments = (process / "cmdline").read_bytes().split(b"\0")
            if any(arg.rsplit(b"/", 1)[-1] == b"klippy.py" for arg in arguments):
                raise ValueError("stop Klipper before using the firmware tool")
            for descriptor in (process / "fd").iterdir():
                try:
                    opened = descriptor.stat()
                except FileNotFoundError:
                    continue
                if stat.S_ISCHR(opened.st_mode) and opened.st_rdev == device_id:
                    raise ValueError("serial device is in use by PID %s" % process.name)
        except FileNotFoundError:
            # A process can exit between listing /proc and inspecting its files.
            continue


@contextmanager
def bus_lock(device):
    check_bus(device)
    import fcntl

    lock = "/var/run/cfs-firmware-%d.lock" % device.stat().st_rdev
    descriptor = os.open(lock, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise ValueError("another firmware tool is using this serial device") from error
        yield
    finally:
        os.close(descriptor)


def run_backend(backend, device, *arguments, expected=None):
    check_bus(device)
    command = [sys.executable, "-u", str(backend), *map(str, arguments),
               "--no-json", "--dev", str(device)]
    if expected is None:
        result = subprocess.run(command, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)
        output = result.stdout.decode("utf-8", errors="replace")
        print(output, end="")
        if result.returncode:
            raise ValueError("firmware backend returned %d" % result.returncode)
        return output
    current, target, image_name = expected
    prompt = "Erase and write %s now? Type YES to flash: " % image_name
    failure = None
    confirmed = False
    # Feed the backend only after checking this subprocess's own loader reply.
    with subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                          stderr=subprocess.STDOUT) as process:
        transcript = bytearray()
        for block in iter(lambda: os.read(process.stdout.fileno(), 4096), b""):
            transcript.extend(block)
            print(block.decode("utf-8", errors="replace"), end="", flush=True)
            if process.stdin.closed or b"Type YES to flash: " not in transcript:
                continue
            output = transcript.decode("utf-8", errors="replace")
            try:
                check_plan(output, current, target)
                if not output.endswith("\n" + prompt):
                    raise ValueError("unexpected firmware confirmation prompt")
                if input() != "YES":
                    raise ValueError("apply cancelled")
                process.stdin.write(b"YES\n")
                process.stdin.flush()
                confirmed = True
            except (ValueError, EOFError, KeyboardInterrupt, OSError) as error:
                failure = error
            finally:
                process.stdin.close()
        code = process.wait()
    if failure is not None:
        raise failure
    if code:
        raise ValueError("firmware backend returned %d. Inspect the transfer before cycling power." % code)
    if not confirmed:
        raise ValueError("firmware backend exited before confirmation")
    return transcript.decode("utf-8", errors="replace")


def check_identity(output, version, serial):
    observed_version = re.findall(r"^\s*Firmware\s*:\s*([0-9.]+)[ \t]*$", output, re.M)
    observed_serial = re.findall(r"^\s*Serial number\s*:\s*([^\r\n]+)$", output, re.M)
    if observed_version != [version_text(version)] or observed_serial != [serial]:
        raise ValueError("CFS application version or serial does not match the expected device")


def check_plan(output, current, target):
    loader = re.findall(r"^[ \t]*->[ \t]+current version: ([^\r\n]+)", output, re.M)
    plan = re.findall(r"^PLAN: (\d\.\d\.\d)  ->  (\d\.\d\.\d)[ \t]+\(", output, re.M)
    if (loader != [repr(BOOT + "-" + APP + current)] or plan != [(version_text(current), version_text(target))]
            or re.search(r"^[ \t]*(?:ABORT|REFUSED|ERROR)\b", output, re.M)):
        raise ValueError("firmware backend did not report the expected loader and transfer plan")


def check_transfer(output, current, target, *, dry_run):
    completion = "DRY RUN complete. NOTHING was written." if dry_run else "=== FLASH END ==="
    if completion not in output or re.search(r"^[ \t]*(?:ABORT|REFUSED|ERROR)\b", output, re.M):
        raise ValueError("transfer did not produce the expected completion report. Inspect the transcript.")
    check_plan(output, current, target)


def run(args):
    if args.action == "verify":
        if args.target_image is not None or args.apply or args.ack_untested_downgrade:
            raise ValueError("verify takes exactly one image and the hardware options")
        current = None
        target, data = image(args.current_image)
        image_path = args.current_image
    else:
        if args.target_image is None:
            raise ValueError("give both the incumbent rollback image and the target image")
        current, _ = image(args.current_image)
        image_path = args.target_image
        target, data = image(image_path)
        if current == target or (target > current) != (args.action == "update"):
            raise ValueError("image versions do not match the requested %s direction" % args.action)
        print("cfs0 G32 %s: %s -> %s" % (args.action, version_text(current), version_text(target)))
        print("Rollback image: %s" % args.current_image)
        if not args.apply:
            print("Offline plan. Both image hashes verified.")
            return
        if args.action == "downgrade" and not args.ack_untested_downgrade:
            raise ValueError("hardware downgrade is unvalidated. Supply --ack-untested-downgrade to proceed")

    if (args.device is None or not args.device.is_absolute() or args.backend is None
            or not args.serial or args.serial == "(none)" or not args.serial.isprintable() or not args.isolated):
        raise ValueError("specify --device, --backend, --serial and --isolated for hardware commands")
    backend_data = checked_bytes(args.backend, BACKEND_SHA256)
    device = args.device.resolve(strict=True)
    with bus_lock(device), TemporaryDirectory(prefix="cfs-firmware-") as directory:
        # Pin the verified bytes for the complete invocation, including backend re-reads.
        backend = Path(directory) / "cfstool.py"
        backend.write_bytes(backend_data)
        if args.action == "verify":
            check_identity(run_backend(backend, device, "diag"), target, args.serial)
            print("CFS application version %s verified on the expected serial." % version_text(target))
            return

        staged = Path(directory) / image_path.name
        staged.write_bytes(data)
        if not sys.stdin.isatty():
            raise ValueError("apply requires an interactive terminal for both confirmations")
        check_identity(run_backend(backend, device, "diag"), current, args.serial)
        check_transfer(run_backend(backend, device, "flash", staged), current, target, dry_run=True)
        print("The dry run ended without writing. If the CFS stayed in the loader, power-cycle it now.")
        if input("One CFS must be physically isolated. Type APPLY to continue: ").strip() != "APPLY":
            raise ValueError("apply cancelled")
        check_identity(run_backend(backend, device, "diag"), current, args.serial)
        output = run_backend(backend, device, "flash", staged, "--flash", expected=(current, target, staged.name))
        check_transfer(output, current, target, dry_run=False)
        print("Transfer completed. Power-cycle the CFS, then run verify with the target image and serial before using it.")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("update", "downgrade", "verify"))
    parser.add_argument("current_image", type=Path, help="incumbent image, or expected image for verify")
    parser.add_argument("target_image", type=Path, nargs="?")
    parser.add_argument("--apply", action="store_true", help="enter the bootloader and request a write")
    parser.add_argument("--device", type=Path, help="absolute CH340 USB-RS485 device path")
    parser.add_argument("--backend", type=Path, help="path to the checksum-pinned CFSTool script")
    parser.add_argument("--serial", help="expected CFS application serial number")
    parser.add_argument("--isolated", action="store_true", help="confirm only the expected CFS is physically connected")
    parser.add_argument("--ack-untested-downgrade", action="store_true")
    try:
        run(parser.parse_args(argv))
    except (OSError, ValueError, EOFError, KeyboardInterrupt) as error:
        print("CFS firmware: %s" % (str(error) or "interrupted"), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
