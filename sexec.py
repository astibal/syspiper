import os
import pwd
import json
import shutil
import tempfile
import importlib.util
import resource
import time
from contextlib import contextmanager
from pathlib import Path


def safe_exec(name: str, script_path: str) -> dict | None:

    def yell(obj: str):
        print(obj + "\n", flush=True)
        time.sleep(0.05)

    if not os.path.isfile(script_path):
        return {"error": f"Script path '{script_path}' does not exist"}

    tempdir = tempfile.mkdtemp(prefix="sandbox_")
    r_fd, w_fd = os.pipe()

    try:
        pid = os.fork()
        if pid == 0:
            # === Child process ===
            try:
                os.close(r_fd)
                os.dup2(w_fd, 1)
                os.dup2(w_fd, 2)
                os.close(w_fd)

                os.umask(0o077)

                # Resource limits
                resource.setrlimit(resource.RLIMIT_CPU, (5, 5))
                resource.setrlimit(resource.RLIMIT_AS, (128 << 20, 128 << 20))  # 128 MB
                resource.setrlimit(resource.RLIMIT_NOFILE, (256, 256))
                resource.setrlimit(resource.RLIMIT_FSIZE, (0, 0))
                resource.setrlimit(resource.RLIMIT_CORE, (0, 0))

                try:
                    nobody = pwd.getpwnam("nobody")
                    os.setuid(nobody.pw_uid)
                except PermissionError:
                    pass  # not root, cannot drop privileges

                # Load script
                spec = importlib.util.spec_from_file_location(name, script_path)

                # Try to chdir to private sandbox dir
                try:
                    os.chdir(tempdir)
                except Exception:
                    os.chdir("/")

                @contextmanager
                def temporary_sys_path(path):
                    original = sys.path.copy()
                    sys.path.insert(0, str(path))
                    try:
                        yield
                    finally:
                        sys.path = original

                with temporary_sys_path(Path(f"{script_path}/lib").resolve()):
                    module = importlib.util.module_from_spec(spec)
                    spec.loader.exec_module(module)

                resource.setrlimit(resource.RLIMIT_NOFILE, (10, 10))  # lock down after import

                if not hasattr(module, "main") or not callable(module.main):
                    yell(json.dumps({"error": "main() not found or not callable"}))
                    os._exit(0)

                # Inside the child fork
                import signal
                def _handle_timeout(signum, frame):
                    yell(json.dumps({"error": "timeout"}))
                    os.exit(1)

                signal.signal(signal.SIGALRM, _handle_timeout)
                signal.alarm(5)  # Enforce wall-clock timeout
                result = module.main()

                if isinstance(result, dict):
                    yell(json.dumps({"result": result}))
                elif isinstance(result, str):
                    yell(json.dumps({"result": { "stdout": result[:256], "trimmed": len(result) > 256 }}))
                else:
                    yell(json.dumps({"error": "main() must return dict"}))

                os._exit(0)

            except (KeyboardInterrupt, SystemExit):
                yell(json.dumps({"error": "interrupted"}))
                os._exit(1)
            except Exception as e:
                yell(json.dumps({"error": str(e)}))
                os._exit(1)

        else:
            # === Parent process ===
            os.close(w_fd)
            output = b""
            with os.fdopen(r_fd, "rb") as pipe:
                while True:
                    chunk = pipe.read(4096)
                    if not chunk:
                        break
                    output += chunk

            _, _ = os.waitpid(pid, 0)

            # Cleanup sandbox directory
            try:
                shutil.rmtree(tempdir)
            except Exception as e:
                pass  # optional: log cleanup failure

            decoded = output.decode("utf-8", errors="replace")

            try:
                return json.loads(decoded)
            except Exception as e:
                return {
                    "error": f"Invalid JSON output: {e}",
                    "raw_output": decoded,
                }

    except Exception as e:
        try:
            shutil.rmtree(tempdir)
        except:
            pass
        return {"error": f"safe_exec internal failure: {e}"}
