"""Copy-paste installation through real Bash with contained dependency commands."""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


class InstallationTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.uv_dir = self.root / "uv commands"
        self.installed = self.root / "installed commands"
        self.installed.mkdir()
        self.log = self.root / "calls.jsonl"
        self.entry = Path(__file__).resolve().parents[1] / "install.sh"
        self.env = {
            **os.environ,
            "PATH": f"{self.bin}:/usr/bin:/bin",
            "UV_INSTALL_DIR": str(self.uv_dir),
            "WAKE_TEST_UV_DIR": str(self.uv_dir),
            "WAKE_TEST_LOG": str(self.log),
            "WAKE_TEST_BIN": str(self.installed),
            "WAKE_TEST_VERSION": "codex-cli 0.148.0",
        }
        self.fake(
            "codex",
            """
if sys.argv[1:]==['--version']:
    print(os.environ['WAKE_TEST_VERSION'])
else:
    print('{}')
""",
        )
        self.fake(
            "uv",
            """
if sys.argv[1:]==['tool','dir','--bin']:
    print(os.environ['WAKE_TEST_BIN'])
elif sys.argv[1:]==['tool','install','--python','3.12','.[mcp]']:
    target=Path(os.environ['WAKE_TEST_BIN'])/'codex-wakeup-mcp'
    target.write_text('#!/bin/sh\\nexit 0\\n')
    target.chmod(0o700)
else:
    raise SystemExit('unexpected installer command')
""",
        )

    def fake(self, name, body):
        command = self.bin / name
        command.write_text(
            f"#!{sys.executable}\n"
            "import json, os, sys\nfrom pathlib import Path\n"
            "with open(os.environ['WAKE_TEST_LOG'],'a') as log:\n"
            f"    log.write(json.dumps({{'command':{name!r},'args':sys.argv[1:],"
            "'cwd':os.getcwd()})+'\\n')\n" + body
        )
        command.chmod(0o700)
        return command

    def run_install(self):
        return subprocess.run(
            ["/bin/bash", str(self.entry)],
            cwd=self.root,
            env=self.env,
            capture_output=True,
            text=True,
            timeout=10,
        )

    def calls(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()]

    def test_installs_from_own_folder_and_registers_absolute_executable(self):
        result = self.run_install()
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.calls()
        install = next(c for c in calls if c["command"] == "uv" and c["args"][1] == "install")
        self.assertEqual(install["cwd"], str(self.entry.parent))
        registration = next(c for c in calls if c["args"][:2] == ["mcp", "add"])
        self.assertEqual(
            registration["args"],
            [
                "mcp",
                "add",
                "codex_wakeup",
                "--env",
                "CODEX_WAKEUP_VERSION=0.5.0",
                "--",
                str(self.installed / "codex-wakeup-mcp"),
            ],
        )
        self.assertEqual(calls[-1]["args"], ["mcp", "get", "codex_wakeup"])
        self.assertTrue(all(c["args"][:2] != ["app-server", "daemon"] for c in calls))

    def test_old_and_prerelease_versions_stop_before_install_or_registration(self):
        for version in ["codex-cli 0.147.0", "codex-cli 0.148.0-alpha.1", "unrecognized"]:
            with self.subTest(version=version):
                self.env["WAKE_TEST_VERSION"] = version
                self.log.unlink(missing_ok=True)
                result = self.run_install()
                self.assertEqual(result.returncode, 1)
                self.assertIn("0.148.0", result.stderr)
                self.assertEqual(
                    self.calls(),
                    [{"command": "codex", "args": ["--version"], "cwd": str(self.root)}],
                )
                self.assertFalse((self.installed / "codex-wakeup-mcp").exists())

    def test_missing_codex_refuses(self):
        (self.bin / "codex").unlink()
        result = self.run_install()
        self.assertEqual(result.returncode, 1)
        self.assertIn("Install Codex", result.stderr)
        self.assertFalse(self.log.exists())

    def test_missing_uv_bootstraps_into_user_directory_without_changing_path_files(self):
        fixture_uv = self.root / "fixture-uv"
        shutil.move(self.bin / "uv", fixture_uv)
        self.env["WAKE_TEST_UV"] = str(fixture_uv)
        self.fake(
            "curl",
            """
assert sys.argv[1:-2] == ['--fail','--silent','--show-error','--location',
    'https://astral.sh/uv/install.sh']
assert sys.argv[-2]=='-o'
Path(sys.argv[-1]).write_text('''#!/bin/sh
set -eu
test "$UV_NO_MODIFY_PATH" = 1
test "$UV_INSTALL_DIR" = "$WAKE_TEST_UV_DIR"
mkdir -p "$UV_INSTALL_DIR"
cp "$WAKE_TEST_UV" "$UV_INSTALL_DIR/uv"
chmod +x "$UV_INSTALL_DIR/uv"
''')
""",
        )
        result = self.run_install()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.uv_dir / "uv").is_file())
        self.assertFalse((self.root / ".profile").exists())
        self.assertTrue(any(c["args"][:2] == ["mcp", "add"] for c in self.calls()))
