import os
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path


@dataclass(slots=True)
class ProcessResult:
    """Small subprocess result wrapper used by the AREDev controller."""

    returncode: int
    stdout: str = ""
    stderr: str = ""


def default_process_runner(
    args: Sequence[str],
    *,
    cwd: Path,
    env: Mapping[str, str] | None,
    capture_output: bool,
    background: bool,
    detach: bool = False,
) -> ProcessResult:
    """Run a subprocess either in the foreground or detached in the background."""

    if background:
        popen_kwargs: dict[str, object] = {
            "cwd": cwd,
            "env": dict(env or os.environ),
        }
        if detach:
            popen_kwargs.update(
                {
                    "stdin": subprocess.DEVNULL,
                    "stdout": subprocess.DEVNULL,
                    "stderr": subprocess.DEVNULL,
                    "close_fds": True,
                }
            )
            if os.name == "nt":
                popen_kwargs["creationflags"] = getattr(
                    subprocess, "DETACHED_PROCESS", 0
                ) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            else:
                popen_kwargs["start_new_session"] = True
        subprocess.Popen(list(args), **popen_kwargs)
        return ProcessResult(returncode=0)

    completed = subprocess.run(
        list(args),
        cwd=cwd,
        env=dict(env or os.environ),
        text=True,
        capture_output=capture_output,
    )
    return ProcessResult(
        returncode=completed.returncode,
        stdout=completed.stdout or "",
        stderr=completed.stderr or "",
    )
