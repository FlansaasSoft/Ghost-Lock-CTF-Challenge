"""Regressions for failures observed during actual C2 provisioning."""
import importlib.util
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("sliver_setup", Path(__file__).resolve().parents[1] / "ansible/files/sliver_setup.py")
setup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(setup)


class SliverSetupTests(unittest.TestCase):
    def test_unprivileged_client_uses_accessible_service_working_directory(self):
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(setup, "HOME", Path(directory)), \
             patch.object(setup.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "ok", "")) as run:
            self.assertEqual(setup.sliver(["client", "version"]), "ok")
            self.assertEqual(run.call_args.kwargs["cwd"], Path(directory))

    def test_command_failure_keeps_diagnostics_without_echoing_them(self):
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(setup, "HOME", Path(directory)), \
             patch.object(setup.subprocess, "run", return_value=subprocess.CompletedProcess([], 1, "", "private diagnostics")):
            with self.assertRaisesRegex(RuntimeError, "private C2 setup log"):
                setup.sliver(["client", "version"])
            self.assertEqual((Path(directory) / "last-command.log").read_text(), "private diagnostics")


if __name__ == "__main__": unittest.main()
