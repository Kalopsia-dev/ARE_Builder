"""Parallel, disposable AREDev test servers ("slots").

Slot N is the Compose project ``aredev-slotN``: the project's
``docker-compose.yml`` plus a generated override that gives the ``db`` and
``nwserver`` services their own containers, database volume, NWN home copy,
log folder and UDP port (``NWN_PORT + N``). The built module and override
content are shared read-only with the main server.

The slot folder doubles as the claim: creating it claims the slot and
``release`` deletes it together with the slot's containers and database.
"""

import argparse
import getpass
import json
import os
import re
import shutil
import stat
import time
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING

from arebuilder.app.aredev.process import ProcessResult
from arebuilder.app.aredev.project import load_env_file

if TYPE_CHECKING:
    from arebuilder.app.aredev.controller import AREDevController

SLOT_COUNT = 4
SLOT_PREFIX = "aredev-slot"
DEFAULT_WAIT_PATTERN = "Server: Module loaded"
DEFAULT_TIMEOUT_SECONDS = 600.0
POLL_SECONDS = 2.0
DB_READY_TIMEOUT_SECONDS = 120.0
ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
SLOT_SUBCOMMANDS = ("claim", "run", "logs", "sql", "install", "status", "release")
SLOT_HELP = f"""\
Usage: slot <command> [arguments]
    claim [label]          Claim a free slot (1-{SLOT_COUNT}) and print its number.
    run N [options]        (Re)start slot N's server and wait until its log matches.
        -e KEY=VALUE        Extra server environment variable (repeatable).
        -v HOST:PATH[:ro]   Extra bind mount (repeatable).
        --wait REGEX        Log line to wait for (default: "{DEFAULT_WAIT_PATTERN}").
        --timeout SECONDS   How long to wait (default: {DEFAULT_TIMEOUT_SECONDS:g}).
    logs N [options]       Show slot N's server log; options are passed to "docker logs".
    sql N QUERY            Run QUERY in slot N's database.
    install N FILE...      Copy files into slot N's development folder.
    status                 List the slots and who claimed them.
    release N              Delete slot N's containers, database and files."""


class SlotError(Exception):
    """A slot command failure reported as one message."""


class _ArgumentParser(argparse.ArgumentParser):
    """Argument parser that raises instead of exiting the process."""

    def error(self, message: str) -> None:
        """Report a parse error as a slot error."""

        raise SlotError(f"{self.prog}: {message}")


class Slots:
    """Claim, run and release slots through an AREDev controller's Docker runners."""

    def __init__(self, controller: "AREDevController"):
        """Use the controller's layout, config, output and Docker runners."""

        self.controller = controller
        self.layout = controller.layout
        self.output = controller.output
        self.root = self.layout.temp_dir / "slots"

    def slot_dir(self, number: int) -> Path:
        """Return the folder holding one slot's files and claim."""

        return self.root / f"s{number}"

    def home_dir(self, number: int) -> Path:
        """Return the private NWN home of one slot."""

        return self.slot_dir(number) / "server"

    def override_file(self, number: int) -> Path:
        """Return the generated Compose override of one slot."""

        return self.slot_dir(number) / "compose.slot.yml"

    def project(self, number: int) -> str:
        """Return the Compose project name of one slot."""

        return f"{SLOT_PREFIX}{number}"

    def container(self, number: int, service: str) -> str:
        """Return the container name of one slot service."""

        return f"{self.project(number)}-{service}"

    def port(self, number: int) -> int:
        """Return slot N's UDP port: the main server's NWN_PORT plus N."""

        env_values = load_env_file(self.layout.nwserver_env_path)
        return int(env_values.get("NWN_PORT", "5121")) + number

    def claim(self, label: str | None) -> int:
        """Claim the first free slot, give it a fresh NWN home and return its number."""

        self.layout.ensure_runtime_dirs()
        self.root.mkdir(parents=True, exist_ok=True)
        for number in range(1, SLOT_COUNT + 1):
            try:
                # mkdir is atomic, so two claimants never get the same slot.
                self.slot_dir(number).mkdir()
            except FileExistsError:
                continue
            try:
                (self.slot_dir(number) / "claim.json").write_text(
                    json.dumps(
                        {
                            "label": label or getpass.getuser(),
                            "claimed": time.strftime("%Y-%m-%d %H:%M"),
                        }
                    ),
                    encoding="utf-8",
                )
                self._copy_home(self.home_dir(number))
                (self.slot_dir(number) / "logs").mkdir()
                self._write_override(number)
            except BaseException:
                remove_tree(self.slot_dir(number))
                raise
            return number
        raise SlotError(f"No free slot: all {SLOT_COUNT} slots are claimed.")

    def release(self, number: int) -> None:
        """Remove a slot's containers, database volume and files."""

        if self.override_file(number).is_file():
            self._check(self._compose(number, ["down", "--remove-orphans"]))
        self._check(
            self._docker(["volume", "rm", "-f", f"{self.project(number)}_database"])
        )
        remove_tree(self.slot_dir(number))
        self.output(f"Slot {number} released.")

    def run(
        self,
        number: int,
        *,
        environment: Sequence[str] = (),
        mounts: Sequence[dict[str, object]] = (),
        wait_pattern: str = DEFAULT_WAIT_PATTERN,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> int:
        """(Re)start a slot's server and wait until its log matches ``wait_pattern``."""

        self._require_claimed(number)
        module = self.controller.config.module_name
        if not self.layout.module_archive_path(module).is_file():
            raise SlotError(f"Module {module}.mod not found. Run 'build' first.")

        self._write_override(number, environment=environment, mounts=mounts)
        self._check(self._compose(number, ["up", "-d", "db"]))
        self._wait_for_database(number)
        self._check(
            self._compose(
                number,
                ["up", "-d", "--no-deps", "--force-recreate", "nwserver"],
                extra_env={"NWN_MODULE": module},
            )
        )
        container = self.container(number, "nwserver")
        self.output(f"{container} started on port {self.port(number)}/udp.")
        self._wait_for_log(
            container, re.compile(wait_pattern), time.monotonic() + timeout
        )
        return 0

    def logs(self, number: int, docker_args: Sequence[str]) -> int:
        """Show a slot server's log through 'docker logs'."""

        return self._docker(
            ["logs", *docker_args, self.container(number, "nwserver")],
            capture_output=False,
        ).returncode

    def sql(self, number: int, query: str) -> int:
        """Run one query in a slot's database."""

        client = (
            'exec mysql -u"$MYSQL_USER" -p"$MYSQL_PASSWORD" "$MYSQL_DATABASE" -e "$1"'
        )
        return self._compose(
            number,
            ["exec", "-T", "db", "sh", "-c", client, "sql", query],
            capture_output=False,
        ).returncode

    def install(self, number: int, files: Sequence[str]) -> None:
        """Copy files into a slot's development folder."""

        self._require_claimed(number)
        development = self.home_dir(number) / "development"
        for name in files:
            shutil.copy2(name, development / Path(name).name)
        self.output(f"Installed {len(files)} file(s) into {development}.")

    def status(self) -> None:
        """Print one line per slot with its port, server state and claim."""

        result = self._docker(
            [
                "ps",
                "-a",
                "--filter",
                f"name={SLOT_PREFIX}",
                "--format",
                "{{.Names}}\t{{.State}}",
            ]
        )
        states = dict(
            line.split("\t", 1) for line in result.stdout.splitlines() if "\t" in line
        )
        self.output(f"{'SLOT':<6}{'PORT':<7}{'SERVER':<10}CLAIM")
        for number in range(1, SLOT_COUNT + 1):
            claim = "free"
            if self.slot_dir(number).is_dir():
                try:
                    info = json.loads(
                        (self.slot_dir(number) / "claim.json").read_text(
                            encoding="utf-8"
                        )
                    )
                    claim = f"{info['label']} since {info['claimed']}"
                except (OSError, ValueError, KeyError):
                    claim = "claimed"
            server = states.get(self.container(number, "nwserver"), "-")
            self.output(f"{number:<6}{self.port(number):<7}{server:<10}{claim}")

    def _require_claimed(self, number: int) -> None:
        """Reject commands for slots that are not claimed."""

        if not self.override_file(number).is_file():
            raise SlotError(f"Slot {number} is not claimed.")

    def _copy_home(self, destination: Path) -> None:
        """Copy the project's NWN home, leaving out content shared with the main server.

        ``modules`` and ``override`` are mounted read-only instead, HAK and TLK
        come from the base Compose file (symlinks to them are kept), and
        live-compiled scripts are left out as ``stop`` does for the main server.
        """

        source = self.layout.server_dir

        def ignore(directory: str, names: list[str]) -> set[str]:
            if Path(directory) == source:
                return {
                    name
                    for name in names
                    if name in {"modules", "override"}
                    or name.endswith(".aredev-shadowed")
                    or (
                        name in {"hak", "tlk"}
                        and not os.path.islink(os.path.join(directory, name))
                    )
                }
            if Path(directory) == self.layout.development_dir:
                return {name for name in names if name.endswith(".ncs")}
            return set()

        shutil.copytree(source, destination, symlinks=True, ignore=ignore)
        for name in ("development", "modules", "override", "hak", "tlk"):
            path = destination / name
            if not path.is_symlink():
                path.mkdir(exist_ok=True)
        nwnplayer = self.layout.are_resources_dir / "config" / "nwnplayer.ini"
        if nwnplayer.is_file():
            shutil.copyfile(nwnplayer, destination / "nwnplayer.ini")

    def _write_override(
        self,
        number: int,
        *,
        environment: Sequence[str] = (),
        mounts: Sequence[dict[str, object]] = (),
    ) -> None:
        """Write the Compose override that turns the main services into slot N's."""

        port = self.port(number)
        environment = [
            f"NWN_PORT={port}",
            f"NWN_SERVERNAME=AREDev slot {number}",
            "NWN_PUBLICSERVER=0",
            # Slots do not start the profiler services, and the metrics plugin
            # aborts the server when its InfluxDB host cannot be resolved.
            "NWNX_METRICS_INFLUXDB_SKIP=y",
            *environment,
        ]
        volumes = [
            _bind(self.slot_dir(number) / "logs", "/nwn/run/logs.0"),
            _bind(self.home_dir(number), "/nwn/home"),
            _bind(self.layout.modules_dir, "/nwn/home/modules", read_only=True),
            _bind(self.layout.override_dir, "/nwn/home/override", read_only=True),
            *mounts,
        ]
        # JSON values are valid YAML flow values; "!override" replaces the base
        # file's port and dependencies instead of merging with them.
        self.override_file(number).write_text(
            f"""\
# Generated by arebuilder for AREDev slot {number}; changes are overwritten.
services:
  db:
    container_name: {_yaml(self.container(number, "db"))}
    restart: "no"
  nwserver:
    container_name: {_yaml(self.container(number, "nwserver"))}
    restart: "no"
    depends_on: !override ["db"]
    ports: !override [{_yaml(f"{port}:{port}/udp")}]
    environment: {_yaml(environment)}
    volumes: {_yaml(volumes)}
""",
            encoding="utf-8",
        )

    def _wait_for_database(self, number: int) -> None:
        """Wait until MariaDB accepts TCP connections, i.e. finished its first-run init."""

        deadline = time.monotonic() + DB_READY_TIMEOUT_SECONDS
        command = ["exec", "-T", "db", "mysqladmin", "ping", "-h127.0.0.1", "--silent"]
        while self._compose(number, command).returncode != 0:
            if time.monotonic() >= deadline:
                raise SlotError(f"Slot {number} database did not become ready.")
            time.sleep(POLL_SECONDS)

    def _wait_for_log(
        self, container: str, pattern: re.Pattern[str], deadline: float
    ) -> None:
        """Wait until a server log matches, failing if the server exits or times out."""

        while True:
            result = self._docker(["logs", container])
            log = ANSI_ESCAPE.sub("", result.stdout + result.stderr)
            if pattern.search(log):
                self.output(f"{container}: matched /{pattern.pattern}/.")
                return
            running = self._docker(["inspect", "-f", "{{.State.Running}}", container])
            if running.stdout.strip() != "true":
                for line in log.splitlines()[-40:]:
                    self.output(line)
                raise SlotError(f"{container} exited.")
            if time.monotonic() >= deadline:
                raise SlotError(
                    f"{container}: /{pattern.pattern}/ not seen in time; "
                    "the server is still running."
                )
            time.sleep(POLL_SECONDS)

    def _compose(
        self,
        number: int,
        args: list[str],
        *,
        extra_env: dict[str, str] | None = None,
        capture_output: bool = True,
    ) -> ProcessResult:
        """Run Docker Compose for one slot project."""

        return self.controller._run_compose(
            args,
            extra_env=extra_env,
            capture_output=capture_output,
            project=self.project(number),
            compose_files=[self.layout.compose_file, self.override_file(number)],
        )

    def _docker(self, args: list[str], *, capture_output: bool = True) -> ProcessResult:
        """Run a container CLI command for the slots."""

        return self.controller._run_container_command(
            args,
            cwd=self.layout.root,
            env=self.controller._compose_env(),
            capture_output=capture_output,
            background=False,
        )

    @staticmethod
    def _check(result: ProcessResult) -> None:
        """Raise a slot error with the output of a failed command."""

        if result.returncode != 0:
            raise SlotError(
                (result.stderr or result.stdout).strip() or "Docker failed."
            )


def run_slot_command(controller: "AREDevController", args: Sequence[str]) -> int:
    """Parse and run one ``slot`` subcommand, returning a status code."""

    slots = Slots(controller)
    try:
        parsed = _parser().parse_args(list(args))
        command = parsed.command
        if command is None:
            slots.output(SLOT_HELP)
            return 1
        if command == "status":
            slots.status()
            return 0
        if command == "claim":
            slots.output(str(slots.claim(parsed.label)))
            return 0
        if not 1 <= parsed.number <= SLOT_COUNT:
            raise SlotError(f"Slot must be between 1 and {SLOT_COUNT}.")
        if command == "run":
            return slots.run(
                parsed.number,
                environment=[_environment(item) for item in parsed.environment],
                mounts=[_mount(item) for item in parsed.mounts],
                wait_pattern=parsed.wait,
                timeout=parsed.timeout,
            )
        if command == "logs":
            return slots.logs(parsed.number, parsed.docker_args)
        if command == "sql":
            return slots.sql(parsed.number, " ".join(parsed.query))
        if command == "install":
            slots.install(parsed.number, parsed.files)
        else:
            slots.release(parsed.number)
        return 0
    except (SlotError, OSError, re.error) as exc:
        slots.output(str(exc))
        return 1


def _parser() -> argparse.ArgumentParser:
    """Build the parser for slot subcommands."""

    parser = _ArgumentParser(prog="slot", add_help=False)
    commands = parser.add_subparsers(dest="command", parser_class=_ArgumentParser)
    claim = commands.add_parser("claim", prog="slot claim", add_help=False)
    claim.add_argument("label", nargs="?")
    commands.add_parser("status", prog="slot status", add_help=False)
    run = commands.add_parser("run", prog="slot run", add_help=False)
    run.add_argument("number", type=int)
    run.add_argument("-e", action="append", default=[], dest="environment")
    run.add_argument("-v", action="append", default=[], dest="mounts")
    run.add_argument("--wait", default=DEFAULT_WAIT_PATTERN)
    run.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_SECONDS)
    logs = commands.add_parser("logs", prog="slot logs", add_help=False)
    logs.add_argument("number", type=int)
    logs.add_argument("docker_args", nargs=argparse.REMAINDER)
    sql = commands.add_parser("sql", prog="slot sql", add_help=False)
    sql.add_argument("number", type=int)
    sql.add_argument("query", nargs="+")
    install = commands.add_parser("install", prog="slot install", add_help=False)
    install.add_argument("number", type=int)
    install.add_argument("files", nargs="+")
    release = commands.add_parser("release", prog="slot release", add_help=False)
    release.add_argument("number", type=int)
    return parser


def _environment(item: str) -> str:
    """Validate one -e KEY=VALUE argument."""

    if not re.match(r"[A-Za-z_][A-Za-z0-9_]*=", item):
        raise SlotError(f"-e needs KEY=VALUE (got {item!r}).")
    return item


def _mount(text: str) -> dict[str, object]:
    """Parse ``HOST:/container/path[:ro]``; splitting on the last colon keeps drive letters."""

    value, read_only = (text[:-3], True) if text.endswith(":ro") else (text, False)
    host, _, target = value.rpartition(":")
    if not host or not target.startswith("/"):
        raise SlotError(f"-v needs HOST:/container/path[:ro] (got {text!r}).")
    if not Path(host).exists():
        raise SlotError(f"No such host path: {host}")
    return _bind(Path(host).resolve(), target, read_only=read_only)


def _bind(source: Path, target: str, *, read_only: bool = False) -> dict[str, object]:
    """Return a long-syntax Compose bind mount."""

    mount: dict[str, object] = {
        "type": "bind",
        "source": source.as_posix(),
        "target": target,
    }
    if read_only:
        mount["read_only"] = True
    return mount


def _yaml(value: object) -> str:
    """Render a value as JSON (valid YAML) with ``$`` escaped from Compose interpolation."""

    return json.dumps(value).replace("$", "$$")


def remove_tree(path: Path) -> None:
    """Delete a directory tree, clearing read-only flags that block removal.

    Copies of a Windows NWN home can carry read-only attributes; on POSIX a
    read-only directory blocks deleting its entries.
    """

    def make_writable_and_retry(function, failed_path, _exc) -> None:
        writable = stat.S_IWRITE | stat.S_IREAD | stat.S_IEXEC
        os.chmod(os.path.dirname(failed_path), writable)
        if os.path.lexists(failed_path) and not os.path.islink(failed_path):
            os.chmod(failed_path, writable)
        function(failed_path)

    if path.exists():
        shutil.rmtree(path, onexc=make_writable_and_retry)
