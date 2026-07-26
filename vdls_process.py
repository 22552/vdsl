"""Bounded, shell-free child process supervision for VDLS."""
from __future__ import annotations

import subprocess
from typing import Any, Sequence


class ProcessInterrupted(Exception):
    pass


class ProcessTimedOut(Exception):
    def __init__(self, argv: Sequence[str], timeout: float):
        super().__init__(f"process timed out after {timeout:g}s: {argv[0]}")
        self.argv=tuple(argv)
        self.timeout=timeout


def _stop(process: subprocess.Popen[Any], grace: float) -> None:
    if process.poll() is not None: return
    process.terminate()
    try: process.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


def run_external(
    argv: Sequence[str], *, timeout: float=300.0, grace: float=2.0,
    cwd: str | None=None, env: dict[str,str] | None=None,
) -> subprocess.CompletedProcess[str]:
    """Run an argv directly and bound cancellation/termination.

    No command shell is involved. SIGINT/KeyboardInterrupt stops the child
    gracefully, escalates after ``grace``, and is converted to a stable marker
    for the CLI's status-130 path.
    """
    command=[str(value) for value in argv]
    process=subprocess.Popen(
        command,stdout=subprocess.PIPE,stderr=subprocess.PIPE,
        text=True,encoding="utf-8",errors="replace",shell=False,
        cwd=cwd,env=env)
    try:
        stdout,stderr=process.communicate(timeout=timeout)
    except KeyboardInterrupt as error:
        _stop(process,grace)
        raise ProcessInterrupted() from error
    except subprocess.TimeoutExpired as error:
        _stop(process,grace)
        raise ProcessTimedOut(command,timeout) from error
    return subprocess.CompletedProcess(
        command,process.returncode,stdout,stderr)
