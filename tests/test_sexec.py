import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch

from app import SysPiper
from sexec import MAX_OUTPUT_BYTES, safe_exec


class ScriptTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.script = self.directory / "check.py"

    def run_script(self, source, **kwargs):
        self.script.write_text(source)
        return safe_exec("check", str(self.script), **kwargs)

    def test_stdout_stderr_and_import_output_do_not_corrupt_result(self):
        result = self.run_script(
            'import os, sys\nprint("import output")\n'
            'def main():\n'
            '    print("stdout output")\n'
            '    print("stderr output", file=sys.stderr)\n'
            '    os.write(1, b"native output\\n")\n'
            '    return {"ok": True}\n'
        )
        self.assertEqual(result["result"], {"ok": True})
        for text in ("import output", "stdout output", "stderr output", "native output"):
            self.assertIn(text, result["output"])

    def test_helper_imports_and_relative_library_access(self):
        library = self.directory / "lib"
        library.mkdir()
        (library / "helper.py").write_text("NUMBER = 42\n")
        (library / "data.txt").write_text("test data")
        result = self.run_script(
            'import helper\nfrom pathlib import Path\n'
            'def main():\n'
            '    return {"number": helper.NUMBER, "text": Path("lib/data.txt").read_text()}\n'
        )
        self.assertEqual(result["result"], {"number": 42, "text": "test data"})
        self.assertFalse((library / "__pycache__").exists())

    def test_import_and_main_have_parent_enforced_timeout(self):
        for source in (
            'import time\ntime.sleep(30)\ndef main():\n    return {}\n',
            'import time, signal\ndef main():\n    signal.signal(signal.SIGALRM, signal.SIG_IGN)\n    time.sleep(30)\n',
            'import os, time\ndef main():\n    if os.fork() == 0:\n        time.sleep(30)\n        os._exit(0)\n    return {}\n',
        ):
            with self.subTest(source=source):
                started = time.monotonic()
                result = self.run_script(source, timeout=0.5)
                self.assertEqual(result, {"error": "timeout"})
                self.assertLess(time.monotonic() - started, 3)

    def test_output_is_drained_but_bounded(self):
        result = self.run_script(
            'import os\ndef main():\n'
            '    for _ in range(128):\n        os.write(1, b"x" * 8192)\n'
            '    return {"ok": True}\n'
        )
        self.assertEqual(result["result"], {"ok": True})
        self.assertEqual(len(result["output"]), MAX_OUTPUT_BYTES)
        self.assertTrue(result["output_trimmed"])

    def test_result_larger_than_pipe_capacity(self):
        result = self.run_script('def main():\n    return {"text": "x" * 200000}\n')
        self.assertEqual(len(result["result"]["text"]), 200000)

    def test_oversized_result_is_rejected(self):
        result = self.run_script('def main():\n    return {"text": "x" * (2 << 20)}\n')
        self.assertIn("exceeds", result["error"])

    def test_string_result_remains_compatible(self):
        result = self.run_script('def main():\n    return "x" * 300\n')
        self.assertEqual(result["result"], {"stdout": "x" * 256, "trimmed": True})

    def test_exceptions_missing_main_and_invalid_results(self):
        for source, message in (
            ('raise ValueError("import failed")\n', "import failed"),
            ('def main():\n    raise ValueError("main failed")\n', "main failed"),
            ('value = 1\n', "main() not found"),
            ('def main():\n    return 1\n', "main() must return"),
            ('def main():\n    return {"bad": object()}\n', "not JSON serializable"),
            ('def main():\n    raise SystemExit(0)\n', "interrupted"),
        ):
            with self.subTest(source=source):
                self.assertIn(message, self.run_script(source)["error"])

    def test_abrupt_exit_returns_error(self):
        result = self.run_script('import os\ndef main():\n    os._exit(7)\n')
        self.assertIn("exit code 7", result["error"])

    def test_working_directory_is_private_and_cleaned(self):
        result = self.run_script('import os\ndef main():\n    return {"cwd": os.getcwd()}\n')
        self.assertNotEqual(result["result"]["cwd"], os.getcwd())
        self.assertFalse(Path(result["result"]["cwd"]).exists())

    def test_runner_preserves_service_identity(self):
        result = self.run_script('import os\ndef main():\n    return {"uid": os.geteuid(), "gid": os.getegid()}\n')
        self.assertEqual(result["result"], {"uid": os.geteuid(), "gid": os.getegid()})

    def test_launch_failure_closes_pipes(self):
        self.script.write_text('def main():\n    return {}\n')
        before = set(os.listdir("/proc/self/fd"))
        with patch("sexec.subprocess.Popen", side_effect=OSError("launch failed")):
            result = safe_exec("check", str(self.script))
        self.assertIn("launch failed", result["error"])
        self.assertEqual(set(os.listdir("/proc/self/fd")), before)

    def test_timeout_reaps_process_and_closes_pipes(self):
        before = set(os.listdir("/proc/self/fd"))
        processes = []
        real_popen = subprocess.Popen

        def spawn(*args, **kwargs):
            process = real_popen(*args, **kwargs)
            processes.append(process)
            return process

        with patch("sexec.subprocess.Popen", side_effect=spawn):
            result = self.run_script('import time\ntime.sleep(30)\n', timeout=0.5)
        self.assertEqual(result, {"error": "timeout"})
        self.assertIsNotNone(processes[0].returncode)
        self.assertEqual(set(os.listdir("/proc/self/fd")), before)

    def test_real_nested_endpoint(self):
        import json
        config = self.directory / "config.json"
        config.write_text(json.dumps({"api_key": "test-key", "allowed_ips": ["127.0.0.1"]}))
        client = SysPiper(str(config)).app.test_client()
        response = client.get("/examples/date", headers={"X-API-Key": "test-key"})
        self.assertEqual(response.status_code, 200)
        self.assertIn("date", response.json)


if __name__ == "__main__":
    unittest.main()
