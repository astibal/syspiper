"""Bounded execution of administrator-trusted scripts, not a security sandbox."""

import importlib.util
import json
import os
from pathlib import Path
import resource
import selectors
import signal
import subprocess
import sys
import tempfile
import time


SCRIPT_TIMEOUT = 5
MAX_RESULT_BYTES = 1 << 20
MAX_OUTPUT_BYTES = 64 << 10


def safe_exec(name: str, script_path: str, *, timeout: float = SCRIPT_TIMEOUT) -> dict:
    """Run a trusted script with a parent-enforced deadline and bounded output.

    Scripts retain the service user's filesystem and network access. Deploy the
    service under its own unprivileged account and install only trusted scripts.
    """
    script_path = Path(script_path).resolve()
    if not script_path.is_file():
        return {"error": f"Script path '{script_path}' does not exist"}
    if os.geteuid() == 0:
        return {"error": "Refusing to run scripts as root; use an unprivileged service account"}

    process = None
    read_fd = write_fd = None
    try:
        with tempfile.TemporaryDirectory(prefix="sandbox_") as tempdir:
            read_fd, write_fd = os.pipe()
            deadline = time.monotonic() + timeout
            try:
                process = subprocess.Popen(
                    [sys.executable, "-B", "-u", str(Path(__file__).resolve()),
                     "--child", str(write_fd), name, str(script_path)],
                    stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    pass_fds=(write_fd,), cwd=tempdir, start_new_session=True,
                )
                os.close(write_fd)
                write_fd = None
                with os.fdopen(read_fd, "rb", buffering=0) as result_pipe, selectors.DefaultSelector() as selector:
                    read_fd = None
                    selector.register(result_pipe, selectors.EVENT_READ, "result")
                    selector.register(process.stdout, selectors.EVENT_READ, "output")
                    buffers = {"result": bytearray(), "output": bytearray()}
                    output_trimmed = False
                    while selector.get_map():
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            return {"error": "timeout"}
                        for key, _ in selector.select(remaining):
                            chunk = os.read(key.fd, 65536)
                            if not chunk:
                                selector.unregister(key.fileobj)
                                continue
                            if key.data == "result":
                                if len(buffers["result"]) + len(chunk) > MAX_RESULT_BYTES:
                                    return {"error": "Script result exceeds 1 MiB limit"}
                                buffers["result"].extend(chunk)
                            else:
                                room = MAX_OUTPUT_BYTES - len(buffers["output"])
                                buffers["output"].extend(chunk[:room])
                                output_trimmed |= len(chunk) > room

                try:
                    process.wait(timeout=max(0, deadline - time.monotonic()))
                except subprocess.TimeoutExpired:
                    return {"error": "timeout"}
                try:
                    result = json.loads(buffers["result"])
                    if not isinstance(result, dict):
                        raise ValueError("Expected a result object")
                except (ValueError, UnicodeDecodeError):
                    result = {"error": f"Script exited without a valid result (exit code {process.returncode})"}
                if buffers["output"]:
                    result["output"] = buffers["output"].decode("utf-8", errors="replace")
                    result["output_trimmed"] = output_trimmed
                return result
            finally:
                if process is not None:
                    # Also stop descendants holding either pipe open after the main script exits.
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    process.wait()
                    process.stdout.close()
                for fd in (read_fd, write_fd):
                    if fd is not None:
                        os.close(fd)
    except Exception as error:
        return {"error": f"Script execution failed: {error}"}


def _run_child(result_fd: int, name: str, script_path: str):
    """Executed in a fresh interpreter, without the web worker's open sockets."""
    try:
        if os.geteuid() == 0:
            raise PermissionError("Refusing to run scripts as root")
        os.umask(0o077)
        resource.setrlimit(resource.RLIMIT_CPU, (5, 5))
        resource.setrlimit(resource.RLIMIT_AS, (128 << 20, 128 << 20))
        resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))
        resource.setrlimit(resource.RLIMIT_FSIZE, (0, 0))
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))

        lib_path = Path(script_path).parent / "lib"
        sys.path.insert(0, str(lib_path))
        Path("lib").symlink_to(lib_path, target_is_directory=True)
        spec = importlib.util.spec_from_file_location(name, script_path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        if not callable(getattr(module, "main", None)):
            raise ValueError("main() not found or not callable")
        value = module.main()
        if isinstance(value, str):
            value = {"stdout": value[:256], "trimmed": len(value) > 256}
        if not isinstance(value, dict):
            raise ValueError("main() must return dict or str")
        payload = json.dumps({"result": value}).encode("utf-8")
    except (KeyboardInterrupt, SystemExit):
        payload = json.dumps({"error": "interrupted"}).encode("utf-8")
    except Exception as error:
        payload = json.dumps({"error": str(error) or type(error).__name__}).encode("utf-8")

    try:
        view = memoryview(payload)
        while view:
            view = view[os.write(result_fd, view):]
    finally:
        os.close(result_fd)
    os._exit(0)


if __name__ == "__main__":
    if len(sys.argv) != 5 or sys.argv[1] != "--child":
        sys.exit("This module is a SysPiper script runner, not a standalone command.")
    _run_child(int(sys.argv[2]), sys.argv[3], sys.argv[4])
