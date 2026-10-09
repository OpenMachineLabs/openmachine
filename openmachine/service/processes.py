"""Bounded service subprocess execution shared by readiness and lifecycle callers."""

import os
import selectors
import signal
import subprocess
import time

from openmachine.foundation.sanitize import Fault


def run(argv: list[str], timeout: float = 30, *, env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    try:
        with subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              cwd="/", env=env, start_new_session=True) as process:
            completed = False
            try:
                buffers = {process.stdout: bytearray(), process.stderr: bytearray()}
                deadline = time.monotonic() + timeout
                with selectors.DefaultSelector() as selector:
                    for stream in buffers:
                        selector.register(stream, selectors.EVENT_READ)
                    while selector.get_map():
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            raise subprocess.TimeoutExpired(argv, timeout)
                        for key, _ in selector.select(min(remaining, 0.1)):
                            data = os.read(key.fd, 8192)
                            if not data:
                                selector.unregister(key.fileobj)
                            else:
                                buffers[key.fileobj].extend(data)
                                if len(buffers[key.fileobj]) > 65536:
                                    raise Fault("service_output_limit", "The service subprocess exceeded its bounded reply; inspect original state.")
                    code = process.wait(timeout=max(0.001, deadline - time.monotonic()))
                completed = True
                return subprocess.CompletedProcess(argv, code, *(bytes(buffers[stream]).decode("utf-8", errors="replace")
                                                               for stream in (process.stdout, process.stderr)))
            finally:
                if not completed:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    process.wait(timeout=5)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise Fault("service_command_unverified", "The bounded service command failed or timed out; inspect status before retrying.") from error
