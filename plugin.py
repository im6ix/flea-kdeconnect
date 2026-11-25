#!/usr/bin/env python3
"""Flea menu action that shares selections through the existing KDE Connect CLI."""

import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile


REQUEST_SECONDS = 25
DISCOVERY_SECONDS = 5
SUBMISSION_RESERVE_SECONDS = 5
CACHE_MAX_AGE_SECONDS = 86400
COPY_CHUNK_BYTES = 1024 * 1024
SECTION_REQUEST_SECONDS = 20
SECTION_DISCOVERY_SECONDS = 5
MOUNT_SECONDS_PER_DEVICE = 8
DEVICE_LINE = re.compile(r"^- (?P<name>.+?): (?P<id>[0-9a-f]{16,64}) on ")


class OperationError(Exception):
    pass


def message(value: object, fallback: str) -> str:
    text = str(value).replace("\x00", " ").strip()
    return (text or fallback)[:1000]


def run_cli(args: list[str], deadline: float) -> subprocess.CompletedProcess:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("KDE Connect did not respond in time.")
    try:
        return subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=remaining,
            check=False,
        )
    except subprocess.TimeoutExpired as error:
        raise TimeoutError("KDE Connect did not respond in time.") from error


def discover_device(deadline: float) -> str:
    remaining = deadline - time.monotonic()
    result = run_cli(
        ["kdeconnect-cli", "--list-available", "--id-only"],
        time.monotonic() + min(DISCOVERY_SECONDS, max(remaining, 0)),
    )
    if result.returncode:
        raise OperationError(
            message(result.stderr or result.stdout, "Could not discover the phone.")
        )
    devices = list(dict.fromkeys(line.strip() for line in result.stdout.splitlines() if line.strip()))
    if not devices:
        raise OperationError("Phone is offline or unreachable. Reconnect it and try again.")
    if len(devices) != 1:
        raise OperationError("Send to phone requires exactly one available paired device.")
    return devices[0]


def validate_paths(raw_paths: object) -> list[Path]:
    if not isinstance(raw_paths, list) or not 1 <= len(raw_paths) <= 512:
        raise OperationError("Select between 1 and 512 files or folders.")
    paths = []
    for raw_path in raw_paths:
        if not isinstance(raw_path, str) or not raw_path or not os.path.isabs(raw_path):
            raise OperationError("Flea must provide absolute selected paths.")
        path = Path(raw_path)
        if not path.exists():
            raise OperationError(f"Selected path does not exist: {raw_path}")
        if not (path.is_file() or path.is_dir()) or not os.access(path, os.R_OK):
            raise OperationError(f"Selected path is not readable: {raw_path}")
        paths.append(path)
    return paths


def check_preparation_deadline(deadline: float) -> None:
    if time.monotonic() >= deadline:
        raise TimeoutError("Folder took too long to prepare. Try a smaller folder.")


def cache_directory() -> Path:
    base = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")
    root = base / "flea-kdeconnect"
    if root.is_symlink():
        raise OperationError(f"Refusing to use symlinked cache directory: {root}")
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    root.chmod(0o700)
    cutoff = time.time() - CACHE_MAX_AGE_SECONDS
    for entry in root.glob("send-*"):
        try:
            if entry.is_symlink() or not entry.is_dir():
                continue
            if entry.stat().st_mtime < cutoff:
                shutil.rmtree(entry)
        except OSError:
            pass
    return root


def archive_folder(source: Path, cache_root: Path, deadline: float) -> Path:
    check_preparation_deadline(deadline)
    cache_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = Path(tempfile.mkdtemp(prefix="send-", dir=cache_root))
    archive = temporary / f"{source.name or 'root'}.zip"

    def raise_walk_error(error: OSError) -> None:
        raise error

    try:
        with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_STORED) as zipped:
            root_name = source.name or "root"
            zipped.writestr(f"{root_name}/", b"")
            for current, directories, filenames in os.walk(
                source, topdown=True, onerror=raise_walk_error, followlinks=False
            ):
                check_preparation_deadline(deadline)
                current_path = Path(current)
                directories[:] = sorted(
                    name
                    for name in directories
                    if not (current_path / name).is_symlink()
                )
                for directory in directories:
                    check_preparation_deadline(deadline)
                    relative = (current_path / directory).relative_to(source)
                    zipped.writestr(f"{(Path(root_name) / relative).as_posix()}/", b"")
                for filename in sorted(filenames):
                    path = current_path / filename
                    relative = path.relative_to(source)
                    archive_name = (Path(root_name) / relative).as_posix()
                    with path.open("rb") as source_file:
                        info = zipfile.ZipInfo(archive_name)
                        with zipped.open(info, "w") as destination:
                            while True:
                                check_preparation_deadline(deadline)
                                chunk = source_file.read(COPY_CHUNK_BYTES)
                                if not chunk:
                                    break
                                destination.write(chunk)
        return archive
    except BaseException:
        shutil.rmtree(temporary, ignore_errors=True)
        raise


def prepare_items(paths: list[Path], cache_root: Path | None, deadline: float) -> list[Path]:
    return [
        archive_folder(path, cache_root, deadline) if path.is_dir() else path
        for path in paths
    ]


def discover_reachable_devices(deadline: float) -> list[tuple[str, str]]:
    result = run_cli(["kdeconnect-cli", "--list-available"], deadline)
    if result.returncode:
        return []
    devices: list[tuple[str, str]] = []
    seen: set[str] = set()
    for line in result.stdout.splitlines():
        match = DEVICE_LINE.match(line.strip())
        if not match:
            continue
        name = match.group("name").strip()[:80]
        device_id = match.group("id")
        if not name or device_id in seen:
            continue
        seen.add(device_id)
        devices.append((name, device_id))
    return devices


def call_sftp(device_id: str, method: str, deadline: float) -> str:
    result = run_cli(
        [
            "busctl",
            "--user",
            "call",
            "org.kde.kdeconnect",
            f"/modules/kdeconnect/devices/{device_id}/sftp",
            "org.kde.kdeconnect.device.sftp",
            method,
        ],
        deadline,
    )
    if result.returncode:
        return ""
    return result.stdout


def mount_device(device_id: str, deadline: float) -> str | None:
    if "b true" not in call_sftp(device_id, "mountAndWait", deadline):
        return None
    match = re.search(r'"([^"]*)"', call_sftp(device_id, "mountPoint", deadline))
    return match.group(1) if match and match.group(1) else None


def handle_section() -> dict:
    deadline = time.monotonic() + SECTION_REQUEST_SECONDS
    try:
        devices = discover_reachable_devices(
            time.monotonic()
            + min(SECTION_DISCOVERY_SECONDS, max(deadline - time.monotonic(), 0))
        )
    except (TimeoutError, OSError):
        return {"op": "section", "rows": []}

    rows = []
    for name, device_id in sorted(devices):
        row_deadline = min(time.monotonic() + MOUNT_SECONDS_PER_DEVICE, deadline)
        try:
            mountpoint = mount_device(device_id, row_deadline)
        except (TimeoutError, OSError):
            mountpoint = None
        fallback = f"/run/user/{os.getuid()}/{device_id}"
        if mountpoint and Path(mountpoint).is_dir():
            storage = Path(mountpoint) / "storage" / "emulated" / "0"
            opens = str(storage) if storage.is_dir() else mountpoint
            rows.append(
                {"id": device_id, "label": name, "glyph": "phone", "state": "on", "opens": opens}
            )
        else:
            rows.append(
                {"id": device_id, "label": name, "glyph": "phone", "state": "off", "opens": fallback}
            )
    return {"op": "section", "rows": rows}


def handle_run(request: dict, emit) -> dict:
    if request.get("action") != "send-to-phone":
        raise OperationError("Unsupported menu action.")
    deadline = time.monotonic() + REQUEST_SECONDS
    paths = validate_paths(request.get("paths"))
    device = discover_device(deadline)

    emit({"op": "progress", "done": 0, "total": len(paths), "message": "Preparing selected items."})
    cache_root = cache_directory() if any(path.is_dir() for path in paths) else None
    prepared = prepare_items(paths, cache_root, deadline - SUBMISSION_RESERVE_SECONDS)
    emit({"op": "progress", "done": len(paths), "total": len(paths), "message": "Submitting to KDE Connect."})
    result = run_cli(
        ["kdeconnect-cli", "--device", device, "--share", *(str(path) for path in prepared)],
        deadline,
    )
    if result.returncode:
        raise OperationError(message(result.stderr or result.stdout, "KDE Connect rejected the selection."))
    count = len(prepared)
    return {"op": "done", "message": f"KDE Connect accepted {count} item{'s' if count != 1 else ''} for your phone."}


def emit(reply: dict) -> None:
    print(json.dumps(reply), flush=True)


def main() -> None:
    for line in sys.stdin:
        try:
            request = json.loads(line)
            if not isinstance(request, dict):
                raise OperationError("Expected a JSON object.")
            operation = request.get("op")
            if operation == "cancel":
                break
            if operation == "hello":
                reply = {"op": "ready", "protocolVersion": 1}
            elif operation == "settings":
                reply = {"op": "done"}
            elif operation == "run":
                reply = handle_run(request, emit)
            elif operation == "section":
                reply = handle_section()
            else:
                raise OperationError("Unsupported request.")
        except (OperationError, TimeoutError, OSError, ValueError) as error:
            reply = {"op": "error", "message": message(error, "Operation failed.")}
        emit(reply)


if __name__ == "__main__":
    main()
