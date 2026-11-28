#!/usr/bin/env python3
"""Inspect known CFS firmware images or prepare a version change."""

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
from typing import NamedTuple


BACKEND_SHA256 = "d8f03fbe0fb127dd5db2fb2323680c713cd39abf22b8ad06b72633e56d409d5c"
IMAGE_SHA256 = {
    "108": "4de0216b2823ab1dae5ab12ff1c6a59a840ff6730f49cb8af3ac95c80a3bfaa9",
    "109": "9c31e30291ff7a88b898c3fe9af77d782d939e602bad820510a5c1426c5ae817",
    "112": "33abcc41d52292bf1e9110012cafcbf61fd25fb887e1e23fefe78ac7b1d545b6",
    "113": "386a1106391a332e6a97803c5ce00b87b6d3ddc4c3d61f6fe07fb19fd3125b76",
    "122": "b20a0b5420cf7cfabf5c2938e1067674b6ceccd7a2d9cf1a2dd3d1a184143e2b",
    "142": "e40c9971b2d487b6710c940ac831545f92fafc5b88a22fe4c7d9e35f0e086439",
    "150": "bde552ef5056037989457e294f88cfa7590b4c9251cda5e40adf0a4fae46c5a3",
    "152": "ed969a32b6f55faed0d41f42d94be6d4fc9ca1598208471dc6179dc0a75435af",
    "153": "5b076563f474da1e8741ee88a9b019c2dcb1b7de6c1c651f201f1be61c345dea",
}
BOOT = "cfs0_050_G32"
APP = "cfs0_000_"
HARDWARE_APPS = frozenset(APP + version for version in ("113", "122", "142", "150", "152", "153"))
CATALOG = {
    "cfs0_050_G30": {APP + version: IMAGE_SHA256[version]
                    for version in ("108", "112", "113", "122", "142", "150", "152")},
    BOOT: {APP + version: digest for version, digest in IMAGE_SHA256.items()},
    "cfs6_100_G31": {
        "cfs6_220_000": "485fc1c4356d6a4a387230bc97861de5ba40d5dbb2958ae5d3226b60aa9632e3",
        "cfs6_221_000": "ae71bf4f6ac1e0448c94c5c7b6fc5f5edf2476f7c55f3f1e9a32264e434234ce",
        "cfs6_222_000": "1190ef6de9e19240316f7f54b6fe8bd8718cdb226c5e1756e23cf458950c9ec0",
        "cfs6_236_000": "3964c5ce8cb1f2849d4ffe9eb8239ca5ec01fc62fd113543a5d1b8b3d957b7a0",
    },
    "cfs6_100_G51": {
        "cfs6_237_000": "1e728afad7cc9f75ddef11484bc5325036df33367ac4136e6a2eb2922b7bd956",
    },
}


class FirmwareImage(NamedTuple):
    loader: str
    application: str
    sha256: str
    data: bytes


def checked_bytes(path, expected):
    if not path.is_file():
        raise ValueError("expected a regular file: %s" % path)
    data = path.read_bytes()
    if hashlib.sha256(data).hexdigest() != expected:
        raise ValueError("SHA-256 mismatch for %s" % path.name)
    return data


def image(path):
    name = path.name
    if name == "cfs0_050_G30-cfs0_000_108 .bin":
        name = "cfs0_050_G30-cfs0_000_108.bin"
    loader, _, application = name[:-4].partition("-")
    digest = CATALOG.get(loader, {}).get(application) if name.endswith(".bin") else None
    if digest is None:
        raise ValueError("not a known CFS firmware image: %s" % path.name)
    return FirmwareImage(loader, application, digest, checked_bytes(path, digest))


def version_text(version):
    return ".".join(version)


def application_text(application):
    return version_text(application[-3:]) if application.startswith(APP) else application


def hardware_eligible(loader, application):
    return loader == BOOT and application in HARDWARE_APPS


def eligibility(loader, application):
    return "hardware workflow available" if hardware_eligible(loader, application) else "offline only"


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


def show_catalog(args):
    if (args.target_image or args.apply or args.device or args.backend or args.serial
            or args.isolated or args.ack_untested_downgrade):
        raise ValueError("catalog and inspect take no hardware options")
    if args.action == "catalog":
        if args.current_image:
            raise ValueError("catalog takes no image argument")
        for loader, applications in sorted(CATALOG.items()):
            for application, digest in sorted(applications.items()):
                print("%s-%s.bin  %s  %s" % (loader, application, digest, eligibility(loader, application)))
        return
    if args.current_image is None:
        raise ValueError("inspect takes one image")
    selected = image(args.current_image)
    print("Loader: %s\nApplication: %s\nSHA-256: %s\nScope: %s" % (
        selected.loader, selected.application, selected.sha256,
        eligibility(selected.loader, selected.application)))


def run(args):
    if args.action in ("catalog", "inspect"):
        show_catalog(args)
        return
    if args.current_image is None:
        raise ValueError("give a known firmware image")
    if args.action == "verify":
        if args.target_image is not None or args.apply or args.ack_untested_downgrade:
            raise ValueError("verify takes exactly one image and the hardware options")
        incumbent = None
        selected = image(args.current_image)
        image_path = args.current_image
    else:
        if args.target_image is None:
            raise ValueError("give both the incumbent rollback image and the target image")
        incumbent = image(args.current_image)
        image_path = args.target_image
        selected = image(image_path)
        if incumbent.loader != selected.loader:
            raise ValueError("incumbent and target images must use the same CFS loader identity")
        if (incumbent.application == selected.application
                or (selected.application > incumbent.application) != (args.action == "update")):
            raise ValueError("image versions do not match the requested %s direction" % args.action)
        print("%s %s: %s -> %s" % (selected.loader, args.action,
                                    application_text(incumbent.application), application_text(selected.application)))
        print("Rollback image: %s" % args.current_image)
        if not args.apply:
            eligible = hardware_eligible(incumbent.loader, incumbent.application) and hardware_eligible(
                selected.loader, selected.application)
            print("Offline plan. Both image hashes verified. %s." % (
                "Hardware workflow available for both images" if eligible else "This transition is offline only"))
            return

    if (not hardware_eligible(selected.loader, selected.application)
            or incumbent is not None and not hardware_eligible(incumbent.loader, incumbent.application)):
        raise ValueError("hardware workflow is limited to the supported cfs0 G32 application versions")
    if args.action == "downgrade" and not args.ack_untested_downgrade:
        raise ValueError("hardware downgrade is unvalidated. Supply --ack-untested-downgrade to proceed")
    if (args.device is None or not args.device.is_absolute() or args.backend is None
            or not args.serial or args.serial == "(none)" or not args.serial.isprintable() or not args.isolated):
        raise ValueError("specify --device, --backend, --serial and --isolated for hardware commands")
    backend_data = checked_bytes(args.backend, BACKEND_SHA256)
    device = args.device.resolve(strict=True)
    target = selected.application[-3:]
    with bus_lock(device), TemporaryDirectory(prefix="cfs-firmware-") as directory:
        # Pin the verified bytes for the complete invocation, including backend re-reads.
        backend = Path(directory) / "cfstool.py"
        backend.write_bytes(backend_data)
        if args.action == "verify":
            check_identity(run_backend(backend, device, "diag"), target, args.serial)
            print("CFS application version %s verified on the expected serial." % version_text(target))
            return

        current = incumbent.application[-3:]
        staged = Path(directory) / image_path.name
        staged.write_bytes(selected.data)
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
    parser.add_argument("action", choices=("catalog", "inspect", "update", "downgrade", "verify"))
    parser.add_argument("current_image", type=Path, nargs="?",
                        help="incumbent image, or one image for inspect or verify")
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
