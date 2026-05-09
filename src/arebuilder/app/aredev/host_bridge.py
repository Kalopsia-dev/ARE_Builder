import os
import time
import uuid
from collections.abc import Mapping
from pathlib import Path

from arebuilder.app.aredev.process import ProcessResult

HOST_LAUNCHER_ENV = "AREDEV_HOST_LAUNCHER"
HOST_COMMAND_DIR = "host-commands"
HOST_LAUNCH_TIMEOUT_SECONDS = 10.0
HOST_DOCKER_TIMEOUT_SECONDS = 600.0
HOST_LAUNCH_POLL_SECONDS = 0.1


def host_path_looks_windows(path: str) -> bool:
    """Return whether a host path string uses Windows path syntax."""

    return "\\" in path or (len(path) >= 2 and path[1] == ":")


def host_bridge_error() -> str:
    """Return the standard message for missing host-launcher bridge support."""

    return (
        "Dockerized AREDev host commands require the host launcher bridge. "
        "Start AREDev through the generated AREDev.sh or AREDev.bat wrapper."
    )


def join_host_path(host_root: str, relative_path: Path) -> str:
    """Join a host root string and relative path without assuming host OS syntax."""

    separator = "\\" if "\\" in host_root else "/"
    return host_root.rstrip("/\\") + separator + separator.join(relative_path.parts)


class HostBridge:
    """Request/response file bridge used by builder containers to ask the host for work."""

    def __init__(self, temp_dir: Path):
        """Initialize the host bridge under the project temp directory."""

        self.command_dir = temp_dir / HOST_COMMAND_DIR

    def available(self) -> bool:
        """Return whether the host launcher has enabled bridge requests."""

        return os.environ.get(HOST_LAUNCHER_ENV) == "1"

    def request(
        self,
        values: Mapping[str, str],
        *,
        timeout_seconds: float,
    ) -> ProcessResult:
        """Write one host-launcher request and wait for its response file."""

        if not self.available():
            return ProcessResult(1, stderr=host_bridge_error())

        request_id = uuid.uuid4().hex
        self.command_dir.mkdir(parents=True, exist_ok=True)
        request_path = self.command_dir / f"host-{request_id}.request"
        response_path = self.command_dir / f"host-{request_id}.response"
        _write_tab_file(
            request_path,
            {
                "REQUEST_ID": request_id,
                **values,
            },
        )
        response = _wait_for_tab_file(
            response_path,
            timeout_seconds=timeout_seconds,
            poll_seconds=HOST_LAUNCH_POLL_SECONDS,
        )
        if response is None:
            request_path.unlink(missing_ok=True)
            return ProcessResult(
                1,
                stderr=(
                    "Timed out waiting for the AREDev host bridge. Start AREDev "
                    "through the generated AREDev.sh or AREDev.bat wrapper."
                ),
            )

        status = response.get("STATUS", "error")
        default_returncode = "0" if status == "ok" else "1"
        try:
            returncode = int(response.get("RETURN_CODE", default_returncode))
        except ValueError:
            returncode = int(default_returncode)
        message = response.get("MESSAGE", "")
        stdout = response.get("STDOUT", "")
        stderr = response.get("STDERR", "") or (message if returncode else "")
        return ProcessResult(returncode=returncode, stdout=stdout, stderr=stderr)

    def compose(
        self,
        args: list[str],
        *,
        extra_env: Mapping[str, str] | None,
    ) -> ProcessResult:
        """Ask the host launcher bridge to run a supported Compose action."""

        action = _compose_host_action(args)
        if action is None:
            return ProcessResult(
                1,
                stderr=f"Unsupported host Compose action from builder container: {args}",
            )
        values = {
            "COMMAND": "compose",
            "ACTION": action,
        }
        if extra_env:
            values.update({key: value for key, value in extra_env.items()})
        return self.request(values, timeout_seconds=HOST_DOCKER_TIMEOUT_SECONDS)


def _compose_host_action(args: list[str]) -> str | None:
    """Map supported Compose argument lists to host-launcher actions."""

    if args == ["up", "-d", "nwserver"]:
        return "up_nwserver"
    if args == ["--progress", "quiet", "down"]:
        return "down_quiet"
    if args == ["down"]:
        return "down"
    if args == ["pull", "--ignore-pull-failures"]:
        return "pull_ignore_failures"
    if args == ["pull"]:
        return "pull"
    return None


def _write_tab_file(path: Path, values: Mapping[str, str]) -> None:
    """Atomically write a tab-delimited host-launcher request or response file."""

    temp_path = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    text = "".join(f"{key}\t{value}\n" for key, value in values.items())
    temp_path.write_text(text, encoding="utf-8", newline="\n")
    temp_path.replace(path)


def _wait_for_tab_file(
    path: Path,
    *,
    timeout_seconds: float,
    poll_seconds: float,
) -> dict[str, str] | None:
    """Wait for a tab-delimited response file and parse it when it appears."""

    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if path.exists():
            values = _read_tab_file(path)
            path.unlink(missing_ok=True)
            return values
        time.sleep(poll_seconds)
    return None


def _read_tab_file(path: Path) -> dict[str, str]:
    """Parse a tab-delimited host-launcher response file."""

    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        key, separator, value = line.partition("\t")
        if separator:
            values[key] = value
    return values
