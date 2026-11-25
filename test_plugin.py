import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
import unittest
import zipfile

import plugin


PLUGIN = Path(__file__).with_name("plugin.py")
HELLO = {
    "op": "hello",
    "protocolVersion": 1,
    "flea": "0.4.3",
    "language": "en",
    "decimal": ".",
    "settings": {},
    "kinds": ["menu-action"],
}
FAKE_CLI = """#!/usr/bin/env python3
import json
import os
from pathlib import Path
import sys

if sys.argv[1:] == ["--list-available", "--id-only"]:
    print(os.environ["FAKE_DEVICES"])
    raise SystemExit(0)
if sys.argv[1:] == ["--list-available"]:
    print(os.environ["FAKE_DEVICE_LINES"])
    raise SystemExit(0)
if "--share" in sys.argv[1:]:
    Path(os.environ["FAKE_ARGV_LOG"]).write_text(json.dumps(sys.argv[1:]))
    status = int(os.environ["FAKE_SHARE_EXIT"])
    if status:
        print("fake KDE Connect failure", file=sys.stderr)
    raise SystemExit(status)
raise SystemExit(2)
"""
FAKE_BUSCTL = """#!/usr/bin/env python3
import json
import os
import sys

arguments = sys.argv[1:]
call_at = arguments.index("call")
object_path = arguments[call_at + 2]
try:
    device_id = object_path.split("/")[4]
except IndexError:
    device_id = ""
failures = set(json.loads(os.environ["FAKE_MOUNT_FAILURES"]))
method = arguments[call_at + 4]
if method == "mountAndWait":
    if device_id in failures:
        raise SystemExit(1)
    print("b true")
elif method == "mountPoint":
    print('s "%s"' % json.loads(os.environ["FAKE_MOUNT_POINTS"]).get(device_id, ""))
raise SystemExit(0)
"""


def run_action(paths: list[str], action: str = "send-to-phone") -> dict:
    return {"op": "run", "action": action, "paths": paths, "settings": {}}


def run_section() -> dict:
    return {"op": "section"}


def run_plugin(
    root: Path,
    requests: list[dict],
    *,
    devices: list[str] | None = None,
    share_exit: int = 0,
    device_lines: list[str] | None = None,
    mount_points: dict[str, str] | None = None,
    mount_failures: list[str] | None = None,
) -> tuple[list[dict], list[str], Path]:
    fake_bin = root / "bin"
    fake_bin.mkdir(parents=True)
    fake_cli = fake_bin / "kdeconnect-cli"
    fake_cli.write_text(FAKE_CLI)
    fake_cli.chmod(0o755)
    fake_busctl = fake_bin / "busctl"
    fake_busctl.write_text(FAKE_BUSCTL)
    fake_busctl.chmod(0o755)

    argv_log = root / "share-argv.json"
    cache_home = root / "cache"
    env = os.environ.copy()
    env.update(
        PATH=f"{fake_bin}:{env['PATH']}",
        FAKE_DEVICES="\n".join(["phone-1"] if devices is None else devices),
        FAKE_DEVICE_LINES="\n".join([] if device_lines is None else device_lines),
        FAKE_SHARE_EXIT=str(share_exit),
        FAKE_MOUNT_POINTS=json.dumps({} if mount_points is None else mount_points),
        FAKE_MOUNT_FAILURES=json.dumps([] if mount_failures is None else mount_failures),
        FAKE_ARGV_LOG=str(argv_log),
        XDG_CACHE_HOME=str(cache_home),
    )
    process = subprocess.run(
        [sys.executable, str(PLUGIN)],
        input="".join(json.dumps(request) + "\n" for request in requests),
        text=True,
        capture_output=True,
        env=env,
        timeout=10,
        check=False,
    )
    if process.returncode:
        raise AssertionError(
            f"plugin exited {process.returncode}; stderr: {process.stderr.strip()}"
        )
    replies = [json.loads(line) for line in process.stdout.splitlines()]
    argv = json.loads(argv_log.read_text()) if argv_log.exists() else []
    return replies, argv, cache_home / "flea-kdeconnect"


class PluginTests(unittest.TestCase):
    def test_single_file_send(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            selected = root / "hello world.txt"
            selected.write_text("hello")

            replies, argv, _ = run_plugin(root, [HELLO, run_action([str(selected)])])

            self.assertEqual(replies[0], {"op": "ready", "protocolVersion": 1})
            self.assertEqual(replies[-1]["op"], "done")
            self.assertEqual(argv[:3], ["--device", "phone-1", "--share"])
            self.assertEqual(argv[3:], [str(selected)])
            self.assertLessEqual(sum(r["op"] == "progress" for r in replies), 2)

    def test_multiple_files_use_one_invocation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            selected = [root / "one.txt", root / "two;$(literal).txt"]
            for path in selected:
                path.write_text(path.name)

            replies, argv, _ = run_plugin(
                root, [HELLO, run_action([str(path) for path in selected])]
            )

            self.assertEqual(replies[-1]["op"], "done")
            self.assertEqual(argv[3:], [str(path) for path in selected])

    def test_invalid_selection(self):
        for case in ("empty", "relative", "missing", "fifo", "wrong-action"):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                valid_file = root / "valid.txt"
                valid_file.write_text("valid")
                action = "send-to-phone"
                if case == "empty":
                    paths = []
                elif case == "relative":
                    paths = ["relative.txt"]
                elif case == "missing":
                    paths = [str(root / "missing.txt")]
                elif case == "fifo":
                    fifo = root / "pipe"
                    os.mkfifo(fifo)
                    paths = [str(fifo)]
                else:
                    paths = [str(valid_file)]
                    action = "wrong-action"

                replies, argv, _ = run_plugin(
                    root, [HELLO, run_action(paths, action)]
                )

                self.assertEqual(replies[-1]["op"], "error")
                self.assertTrue(replies[-1]["message"])
                self.assertEqual(argv, [])

    def test_folder_zip_and_cleanup(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            folder = root / "My Folder"
            nested = folder / "nested"
            nested.mkdir(parents=True)
            (folder / "empty").mkdir()
            original_bytes = b"nested note contents\n"
            source_file = nested / "note.txt"
            source_file.write_bytes(original_bytes)
            (nested / "loop").symlink_to(folder, target_is_directory=True)

            cache_root = root / "cache" / "flea-kdeconnect"
            stale = cache_root / "send-stale"
            recent = cache_root / "send-recent"
            stale.mkdir(parents=True)
            recent.mkdir()
            old = time.time() - 86401
            os.utime(stale, (old, old))

            replies, argv, returned_cache = run_plugin(
                root, [HELLO, run_action([str(folder)])]
            )

            self.assertEqual(replies[-1]["op"], "done")
            self.assertEqual(returned_cache, cache_root)
            archive = Path(argv[3])
            self.assertEqual(archive.name, "My Folder.zip")
            with zipfile.ZipFile(archive) as zipped:
                names = set(zipped.namelist())
                self.assertGreaterEqual(
                    names,
                    {
                        "My Folder/",
                        "My Folder/nested/",
                        "My Folder/nested/note.txt",
                        "My Folder/empty/",
                    },
                )
                self.assertEqual(
                    zipped.read("My Folder/nested/note.txt"), original_bytes
                )
                self.assertFalse(any("loop" in name for name in names))
            self.assertFalse(stale.exists())
            self.assertTrue(recent.exists())
            self.assertNotEqual(archive.parent, stale)
            self.assertEqual(source_file.read_bytes(), original_bytes)

    def test_archive_deadline_removes_incomplete_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            folder = root / "Folder"
            folder.mkdir()
            (folder / "file.txt").write_text("contents")
            cache_root = root / "cache"
            cache_root.mkdir()

            archive_folder = getattr(plugin, "archive_folder", None)
            self.assertIsNotNone(archive_folder)
            with self.assertRaises(TimeoutError):
                archive_folder(folder, cache_root, time.monotonic() - 1.0)
            self.assertEqual(list(cache_root.glob("send-*")), [])

    def test_operation_error_does_not_end_session(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            selected = root / "valid.txt"
            selected.write_text("valid")
            requests = [
                HELLO,
                run_action(["relative.txt"]),
                run_action([str(selected)]),
            ]

            replies, _, _ = run_plugin(root, requests)

            final_ops = [r["op"] for r in replies if r["op"] in {"error", "done"}]
            self.assertEqual(final_ops, ["error", "done"])
            self.assertEqual(sum(r["op"] == "ready" for r in replies), 1)

    def test_device_discovery_and_cli_failure(self):
        cases = (
            ("offline", [], 0),
            ("multiple", ["one", "two"], 0),
            ("cli-failure", ["phone-1"], 1),
        )
        for case, devices, share_exit in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                selected = root / "file.txt"
                selected.write_text("contents")

                replies, argv, _ = run_plugin(
                    root,
                    [HELLO, run_action([str(selected)])],
                    devices=devices,
                    share_exit=share_exit,
                )

                self.assertEqual(replies[-1]["op"], "error")
                self.assertNotIn("done", [r["op"] for r in replies[1:]])
                if case == "cli-failure":
                    self.assertEqual(argv[:3], ["--device", "phone-1", "--share"])
                else:
                    self.assertEqual(argv, [])


    def test_section_lists_mounted_phone(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            mount = root / "mounts" / "phone-1"
            (mount / "storage" / "emulated" / "0").mkdir(parents=True)

            replies, _, _ = run_plugin(
                root,
                [HELLO, run_section()],
                device_lines=[
                    "- Galaxy A56 5G: 04ac1ec2ffd747e490926db980d7d642"
                    " on 192.168.1.102 via LAN (paired and reachable)"
                ],
                mount_points={"04ac1ec2ffd747e490926db980d7d642": str(mount)},
            )

            self.assertEqual(replies[-1]["op"], "section")
            self.assertEqual(
                replies[-1]["rows"],
                [
                    {
                        "id": "04ac1ec2ffd747e490926db980d7d642",
                        "label": "Galaxy A56 5G",
                        "glyph": "phone",
                        "state": "on",
                        "opens": str(mount / "storage" / "emulated" / "0"),
                    }
                ],
            )

    def test_section_without_devices_is_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            replies, _, _ = run_plugin(Path(tmp), [HELLO, run_section()])

            self.assertEqual(replies[-1], {"op": "section", "rows": []})

    def test_section_sorts_devices_by_name_and_skips_garbage(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            mount_zed = root / "mounts" / "zed"
            mount_alpha = root / "mounts" / "alpha"
            mount_zed.mkdir(parents=True)
            mount_alpha.mkdir(parents=True)

            replies, _, _ = run_plugin(
                root,
                [HELLO, run_section()],
                device_lines=[
                    "- Zed Phone: 04ac1ec2ffd747e490926db980d7d642"
                    " on 192.168.1.102 via LAN (paired and reachable)",
                    "- Alpha Pad: 11aabbccddeeff0011aabbccddeeff00"
                    " on 192.168.1.103 via LAN (paired and reachable)",
                    "not a device line",
                    "- Broken Line: nothex on 192.168.1.104 via LAN (paired and reachable)",
                ],
                mount_points={
                    "04ac1ec2ffd747e490926db980d7d642": str(mount_zed),
                    "11aabbccddeeff0011aabbccddeeff00": str(mount_alpha),
                },
            )

            self.assertEqual(replies[-1]["op"], "section")
            self.assertEqual(
                [row["label"] for row in replies[-1]["rows"]],
                ["Alpha Pad", "Zed Phone"],
            )

    def test_section_marks_unmountable_device_off(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            device_id = "04ac1ec2ffd747e490926db980d7d642"

            replies, _, _ = run_plugin(
                root,
                [HELLO, run_section()],
                device_lines=[
                    f"- Galaxy A56 5G: {device_id}"
                    " on 192.168.1.102 via LAN (paired and reachable)"
                ],
                mount_failures=[device_id],
            )

            self.assertEqual(replies[-1]["op"], "section")
            rows = replies[-1]["rows"]
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["state"], "off")
            self.assertEqual(rows[0]["opens"], f"/run/user/{os.getuid()}/{device_id}")

    def test_section_falls_back_to_mount_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            mount = root / "mounts" / "phone-1"
            mount.mkdir(parents=True)

            replies, _, _ = run_plugin(
                root,
                [HELLO, run_section()],
                device_lines=[
                    "- Galaxy A56 5G: 04ac1ec2ffd747e490926db980d7d642"
                    " on 192.168.1.102 via LAN (paired and reachable)"
                ],
                mount_points={"04ac1ec2ffd747e490926db980d7d642": str(mount)},
            )

            self.assertEqual(replies[-1]["rows"][0]["opens"], str(mount))


if __name__ == "__main__":
    unittest.main()
