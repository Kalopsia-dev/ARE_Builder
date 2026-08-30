import json
import os
import shutil
import subprocess
import time
from importlib import resources
from pathlib import Path

import pytest

from arebuilder.app.aredev.scaffold import (
    ScaffoldConflictError,
    initialize_aredev_project,
)
from arebuilder.app.cli import main


class _ExitPromptSession:
    """Prompt-session test double that immediately asks the menu to exit."""

    def prompt(self, _prompt: str) -> str:
        """Simulate one interactive prompt read for controller tests."""

        return "exit"


def _pretend_host_platform(monkeypatch, platform: str) -> None:
    """Run scaffold init as though it were executing on one host platform."""

    monkeypatch.setattr("arebuilder.app.aredev.scaffold.sys.platform", platform)


def _assert_uses_crlf_line_endings(data: bytes) -> None:
    """Assert that Windows command scripts use CRLF without stray LF separators."""

    assert b"\r\n" in data
    assert b"\n" not in data.replace(b"\r\n", b"")


def _env_values(text: str) -> dict[str, str]:
    """Parse simple KEY=VALUE env-file lines into a dictionary."""

    values: dict[str, str] = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        key, _, value = stripped.partition("=")
        values[key] = value
    return values


def _bind_blocks_for_target(compose_text: str, target: str) -> list[str]:
    """Extract bind-mount blocks for one service target from Compose YAML text."""

    blocks: list[list[str]] = []
    current_block: list[str] = []

    for line in compose_text.splitlines():
        if line == "      - type: bind":
            if current_block:
                blocks.append(current_block)
            current_block = [line]
            continue
        if current_block and line.startswith("        "):
            current_block.append(line)
            continue
        if current_block:
            blocks.append(current_block)
            current_block = []

    if current_block:
        blocks.append(current_block)

    return [
        "\n".join(block)
        for block in blocks
        if any(line.strip() == f"target: {target}" for line in block)
    ]


def _compose_service_block(compose_text: str, service: str) -> str:
    """Extract one generated top-level Compose service block."""

    lines = compose_text.splitlines()
    start = lines.index(f"  {service}:")
    end = len(lines)
    for index in range(start + 1, len(lines)):
        if lines[index] and not lines[index].startswith("    "):
            end = index
            break
    return "\n".join(lines[start:end])


def _assert_compose_uses_minimal_builder_mounts(compose_text: str) -> None:
    """Assert generated Compose text exposes the intended builder and NWN mounts."""

    targets = [
        line.strip().removeprefix("target: ")
        for line in compose_text.splitlines()
        if line.strip().startswith("target: ")
    ]

    builder_root_blocks = _bind_blocks_for_target(compose_text, "/var/builder")
    assert len(builder_root_blocks) == 2
    assert all(
        "source: ${AREDEV_HOST_ROOT:-.}" in block for block in builder_root_blocks
    )
    assert any("read_only: true" in block for block in builder_root_blocks)
    assert any("read_only: true" not in block for block in builder_root_blocks)

    assert "/nwn/install" in targets
    assert "/nwn/home/hak" in targets
    assert "/nwn/home/tlk" in targets
    assert "source: ${AREDEV_NWN_INSTALL_ROOT:-${NWN_INSTALL_PATH}}" in compose_text
    assert (
        "AREDEV_NWN_INSTALL_ROOT: ${AREDEV_NWN_INSTALL_ROOT:-${NWN_INSTALL_PATH}}"
        in compose_text
    )
    assert (
        "AREDEV_NWN_HOME_ROOT: ${AREDEV_NWN_HOME_ROOT:-${NWN_HOME_PATH:-./server}}"
        in compose_text
    )
    assert (
        "source: ${AREDEV_NWN_HOME_ROOT:-${NWN_HOME_PATH:-./server}}/hak"
        in compose_text
    )
    assert "NWN_ROOT: /nwn/install" in compose_text
    assert "HAK_DIR: /nwn/home/hak" in compose_text
    assert "TLK_DIR: /nwn/home/tlk" in compose_text


def test_init_creates_expected_scaffold_without_resource_artifacts(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Verify that init creates expected scaffold without resource artifacts."""

    _pretend_host_platform(monkeypatch, "linux")
    project = tmp_path / "AREDev"

    result = initialize_aredev_project(project)

    assert result.root == project.resolve()
    assert project / "AREDev.sh" in result.created_files
    assert project / "AREDev.bat" not in result.created_files
    for relative_path in [
        "AREDev.sh",
        "docker-compose.yml",
        "config/arebuilder.env",
        "config/db.env",
        "config/nwserver.env",
        "data/bin/aredev-host-launcher.sh",
        "data/help.txt",
        "data/logo.txt",
        "data/timeinit.sql",
    ]:
        assert (project / relative_path).is_file()
    assert not (project / "AREDev.bat").exists()
    assert not (project / "data" / "bin" / "aredev-host-launcher.ps1").exists()

    for relative_path in [
        "temp",
        "compiled-resources",
        "logs",
        "server/development",
        "server/hak",
        "server/localvault",
        "server/modules",
        "server/override",
        "server/servervault",
        "server/tlk",
    ]:
        assert (project / relative_path).is_dir()

    arebuilder_env = _env_values(
        (project / "config" / "arebuilder.env").read_text(encoding="utf-8")
    )
    compose_text = (project / "docker-compose.yml").read_text(encoding="utf-8")
    dockerfile_text = Path("Dockerfile").read_text(encoding="utf-8")
    assert "FROM python:3.14-alpine" in dockerfile_text
    assert "libnwn-musl-compat.so" in dockerfile_text
    assert "LD_PRELOAD=/usr/local/lib/libnwn-musl-compat.so" in dockerfile_text
    assert arebuilder_env["BUILD_TARGET"] == "pgcc"
    assert arebuilder_env["BUILDER_BACKEND"] == "native"
    assert (
        arebuilder_env["AREBUILDER_REPO"]
        == "https://github.com/Kalopsia-dev/ARE_Builder.git"
    )
    assert arebuilder_env["BUILDER_IMAGE"] == (
        "docker.io/kalopsiadev/arebuilder:latest"
    )
    assert arebuilder_env["NWSERVER_IMAGE"] == (
        "docker.io/dmhoodoo/aredevnwnxserver:latest"
    )
    assert "DOCKER_EXECUTABLE" in arebuilder_env
    env_template = resources.files("arebuilder").joinpath(
        "templates", "aredev", "config", "arebuilder.env"
    )
    assert env_template.is_file()
    assert "working_dir: /var/builder" in compose_text
    assert "AREDEV_IN_CONTAINER" in compose_text
    assert "AREDEV_HOST_ROOT" in compose_text
    assert "AREDEV_CONFIG_ROOT" in compose_text
    assert "AREDEV_NWN_INSTALL_ROOT" in compose_text
    assert "AREDEV_HOST_LAUNCHER" in compose_text
    for image in (
        "docker.io/library/mariadb:10",
        "docker.io/library/influxdb:1.12.4",
        "docker.io/grafana/grafana:13.1.1",
        "docker.io/dmhoodoo/aredevnwnxserver:latest",
        "docker.io/kalopsiadev/arebuilder:latest",
    ):
        assert image in compose_text
    _assert_compose_uses_minimal_builder_mounts(compose_text)

    assert not (project / "are-resources").exists()
    assert not (project / "pgcc-resources").exists()
    assert list((project / "server" / "hak").iterdir()) == []
    assert list((project / "server" / "tlk").iterdir()) == []
    shell_wrapper = (project / "AREDev.sh").read_text(encoding="utf-8")
    assert "scaffold_root=" in shell_wrapper
    assert "aredev-host-launcher.sh" in shell_wrapper
    assert 'exec "$launcher" run "$scaffold_root"' in shell_wrapper
    assert "AREDEV_ROOT=${AREDEV_ROOT:-$scaffold_root}" in shell_wrapper
    assert 'exec aredev --root "$AREDEV_ROOT"' in shell_wrapper
    shell_launcher = (project / "data" / "bin" / "aredev-host-launcher.sh").read_text(
        encoding="utf-8"
    )
    for command in [
        "container_status",
        "compose",
        "down_timeout",
        "down_quiet",
        "pull_ignore_failures",
        "volume_drop",
        "update_restart",
    ]:
        assert command in shell_launcher
    assert "run_docker_session" in shell_launcher
    assert "prepare_docker_session" in shell_launcher
    assert "resolve_docker_runtime_paths" in shell_launcher
    assert "ensure_docker_ready" in shell_launcher
    assert "DOCKER_EXECUTABLE" in shell_launcher
    assert "nohup" in shell_launcher
    assert "run_bridge_loop" in shell_launcher
    assert "run_update_session" in shell_launcher
    assert "aredev-host-launcher.sh prepare <project-root>" in shell_launcher
    assert 'prepare) prepare_docker_session "$@"' in shell_launcher
    assert 'update) run_update_session "$@"' in shell_launcher
    assert '"$launcher" bridge "$AREDEV_ROOT" &' in shell_launcher
    assert "--ignore-pull-failures" in shell_launcher
    assert "down --timeout 1" in shell_launcher
    assert "sleep 0.25" in shell_launcher
    assert 'client_dir=$(dirname "$client")' in shell_launcher
    assert 'cd "$client_dir"' in shell_launcher
    assert os.access(project / "AREDev.sh", os.X_OK)
    assert os.access(
        project / "data" / "bin" / "aredev-host-launcher.sh",
        os.X_OK,
    )


@pytest.mark.skipif(os.name == "nt", reason="POSIX host launcher")
@pytest.mark.parametrize(
    ("mode", "launcher_state"),
    [
        ("player", "valid"),
        ("dm", "valid"),
        ("player", "missing"),
        ("player", "non-executable"),
    ],
)
def test_posix_host_bridge_nwn_launcher(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
    launcher_state: str,
) -> None:
    """Exercise actual bridge requests without Docker or the game installed."""

    _pretend_host_platform(monkeypatch, "linux")
    project = tmp_path / "AREDev"
    initialize_aredev_project(project)
    client = project / "launchers" / "custom launcher"
    client.parent.mkdir()
    client.write_text(
        '#!/bin/sh\nprintf "%s\\n" "$PWD" "$@" > "$LAUNCH_LOG"\n',
        encoding="utf-8",
    )
    client.chmod(0o755)
    install = tmp_path / "install"
    install.mkdir()
    configured_path = str(client)
    invalid = launcher_state != "valid"
    if launcher_state == "missing":
        configured_path = str(project / "missing-launcher")
    elif launcher_state == "non-executable":
        client.chmod(0o644)
    config = project / "config" / "arebuilder.env"
    config.write_text(f'NWN_LAUNCHER_PATH="{configured_path}"\n', encoding="utf-8")
    commands = project / "temp" / "host-commands"
    commands.mkdir(parents=True)
    (commands / "launch.request").write_text(
        f"COMMAND\tnwn\nMODE\t{mode}\nPORT\t5129\nPASSWORD\tspace ; $literal\n",
        encoding="utf-8",
    )
    launch_log = tmp_path / "launch.log"
    process = subprocess.Popen(
        [
            "sh",
            str(project / "data" / "bin" / "aredev-host-launcher.sh"),
            "bridge",
            str(project),
            str(install),
        ],
        env={**os.environ, "LAUNCH_LOG": str(launch_log)},
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    response = commands / "launch.response"
    try:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if response.exists() and (invalid or launch_log.exists()):
                break
            if process.poll() is not None:
                break
            time.sleep(0.02)
    finally:
        (commands / "stop").touch()
        try:
            stdout, stderr = process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate()
            raise

    assert process.returncode == 0, stdout + stderr
    response_text = response.read_text(encoding="utf-8")
    if invalid:
        assert "STATUS\terror" in response_text
        assert "NWN_LAUNCHER_PATH" in response_text
        assert not launch_log.exists()
    else:
        assert "STATUS\tok" in response_text
        assert launch_log.read_text(encoding="utf-8").splitlines() == [
            str(client.parent),
            *(["-dmc"] if mode == "dm" else []),
            "+connect",
            "127.0.0.1:5129",
            "+password",
            "space ; $literal",
        ]


def test_linux_host_launcher_uses_podman_when_docker_is_unhealthy(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Verify a broken Docker installation does not mask working Podman."""

    _pretend_host_platform(monkeypatch, "linux")
    project = tmp_path / "AREDev"
    initialize_aredev_project(project)
    nwn_install = tmp_path / "nwn-install"
    nwn_home = tmp_path / "nwn-home"
    nwn_install.mkdir()
    nwn_home.mkdir()
    config_path = project / "config" / "arebuilder.env"
    config_path.write_text(
        config_path.read_text(encoding="utf-8")
        + (
            f"\nNWN_INSTALL_PATH={nwn_install}\n"
            f"NWN_HOME_PATH={nwn_home}\n"
            "DOCKER_EXECUTABLE=\n"
        ),
        encoding="utf-8",
    )

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    runtime_log = tmp_path / "runtime.log"
    docker = fake_bin / "docker"
    docker.write_text(
        '#!/bin/sh\nprintf "docker %s\\n" "$*" >> "$RUNTIME_LOG"\nexit 1\n',
        encoding="utf-8",
    )
    docker.chmod(0o755)
    podman = fake_bin / "podman"
    podman.write_text(
        '#!/bin/sh\nprintf "podman %s|%s|%s\\n" "$*" "${PODMAN_COMPOSE_PROVIDER:-}" "${PODMAN_COMPOSE_WARNING_LOGS:-}" >> "$RUNTIME_LOG"\n',
        encoding="utf-8",
    )
    podman.chmod(0o755)
    podman_compose = fake_bin / "podman-compose"
    podman_compose.write_text("#!/bin/sh\n", encoding="utf-8")
    podman_compose.chmod(0o755)
    for command in ("mkdir", "rm", "rmdir", "uname"):
        executable = shutil.which(command)
        assert executable is not None
        (fake_bin / command).symlink_to(executable)

    shell = shutil.which("sh")
    assert shell is not None
    completed = subprocess.run(
        [
            shell,
            str(project / "data" / "bin" / "aredev-host-launcher.sh"),
            "prepare",
            str(project),
        ],
        env={
            **os.environ,
            "PATH": str(fake_bin),
            "RUNTIME_LOG": str(runtime_log),
        },
        text=True,
        capture_output=True,
    )

    assert completed.returncode == 0, completed.stderr

    updated = subprocess.run(
        [
            shell,
            str(project / "data" / "bin" / "aredev-host-launcher.sh"),
            "update",
            str(project),
        ],
        env={
            **os.environ,
            "PATH": str(fake_bin),
            "RUNTIME_LOG": str(runtime_log),
        },
        text=True,
        capture_output=True,
    )

    assert updated.returncode == 0, updated.stderr
    assert runtime_log.read_text(encoding="utf-8").splitlines() == [
        "docker info",
        "podman info||",
        "docker info",
        "podman info||",
        (
            f"podman compose --env-file {config_path} -p aredev down"
            f"|{podman_compose}|false"
        ),
        (
            "podman compose --env-file "
            f"{config_path} -p aredev pull --ignore-pull-failures"
            f"|{podman_compose}|false"
        ),
    ]


def test_init_generates_profiler_dashboard_assets(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Verify init provisions only the general NWNX Profiler dashboard."""

    _pretend_host_platform(monkeypatch, "linux")
    project = tmp_path / "AREDev"

    initialize_aredev_project(project)

    grafana_config = project / "config" / "grafana"
    datasource_path = (
        grafana_config / "provisioning" / "datasources" / "nwnx.yml"
    )
    provider_path = grafana_config / "provisioning" / "dashboards" / "nwnx.yml"
    dashboard_path = grafana_config / "dashboards" / "nwnx-profiler.json"
    assert datasource_path.is_file()
    assert provider_path.is_file()
    assert dashboard_path.is_file()

    datasource_text = datasource_path.read_text(encoding="utf-8")
    assert "type: influxdb" in datasource_text
    assert "access: proxy" in datasource_text
    assert "uid: nwnx-profiler" in datasource_text
    assert "url: http://influxdb:8086" in datasource_text
    assert "database: nwn" in datasource_text
    assert "isDefault: true" in datasource_text
    assert "httpMode: GET" in datasource_text

    provider_text = provider_path.read_text(encoding="utf-8")
    assert "path: /var/lib/grafana/dashboards" in provider_text
    assert "allowUiUpdates: true" in provider_text
    assert "\n    editable:" not in provider_text

    dashboard = json.loads(dashboard_path.read_text(encoding="utf-8"))
    assert dashboard["uid"] == "nwnx-profiler"
    assert {panel["title"] for panel in dashboard["panels"]} == {
        "Game tick rate",
        "AI event queue depth",
        "AI update-list objects",
        "Main loop work",
        "AI master work",
        "Creature AI work",
        "Creature event-handler work",
        "NWScript work",
        "Pathing work",
    }
    assert {
        panel["datasource"]["uid"] for panel in dashboard["panels"]
    } == {"nwnx-profiler"}
    assert {
        target["datasource"]["uid"]
        for panel in dashboard["panels"]
        for target in panel["targets"]
    } == {"nwnx-profiler"}
    assert {
        panel["title"]: panel["targets"][0]["query"]
        for panel in dashboard["panels"]
    } == {
        "Game tick rate": (
            'SELECT mean("Count") FROM "NWNX_Profiler.GameTickRate" '
            "WHERE $timeFilter GROUP BY time(1s) fill(null)"
        ),
        "AI event queue depth": (
            'SELECT mean("Count") FROM "NWNX_Profiler.AIQueuedEvents" '
            "WHERE $timeFilter GROUP BY time(1s) fill(null)"
        ),
        "AI update-list objects": (
            'SELECT mean("Count") FROM "NWNX_Profiler.AIUpdateListObjects" '
            'WHERE $timeFilter GROUP BY time(1s), "Level" fill(null)'
        ),
        "Main loop work": (
            'SELECT mean("ns") / 1000000 FROM "NWNX_Profiler.TimingEvent" '
            "WHERE $timeFilter AND \"EventName\" = 'ExoAppMainLoop' "
            "GROUP BY time(1s) fill(null)"
        ),
        "AI master work": (
            'SELECT mean("ns") / 1000000 FROM "NWNX_Profiler.TimingEvent" '
            "WHERE $timeFilter AND \"EventName\" = 'AIMasterUpdateState' "
            "GROUP BY time(1s) fill(null)"
        ),
        "Creature AI work": (
            'SELECT mean("ns") / 1000000 FROM "NWNX_Profiler.TimingEvent" '
            "WHERE $timeFilter AND \"EventName\" = 'AIUpdateCreature' "
            "GROUP BY time(1s) fill(null)"
        ),
        "Creature event-handler work": (
            'SELECT mean("ns") / 1000000 FROM "NWNX_Profiler.TimingEvent" '
            "WHERE $timeFilter AND \"EventName\" = 'EventHandlerCreature' "
            "GROUP BY time(1s) fill(null)"
        ),
        "NWScript work": (
            'SELECT sum("ns") / 1000000 FROM "NWNX_Profiler.TimingEvent" '
            "WHERE $timeFilter AND \"EventName\" = 'RunScript' "
            "GROUP BY time(1s) fill(null)"
        ),
        "Pathing work": (
            'SELECT sum("ns") / 1000000 FROM "NWNX_Profiler.TimingEvent" '
            "WHERE $timeFilter AND \"EventName\" = 'PlotPath' "
            "GROUP BY time(1s) fill(null)"
        ),
    }


def test_init_configures_profiler_monitoring_services(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Verify generated services export NWNX Profiler data only to local Grafana."""

    _pretend_host_platform(monkeypatch, "linux")
    project = tmp_path / "AREDev"

    initialize_aredev_project(project)

    compose_text = (project / "docker-compose.yml").read_text(encoding="utf-8")
    assert "  influxdb:\n" in compose_text
    assert "  grafana:\n" in compose_text
    influxdb = _compose_service_block(compose_text, "influxdb")
    grafana = _compose_service_block(compose_text, "grafana")
    nwserver = _compose_service_block(compose_text, "nwserver")
    assert "image: docker.io/library/influxdb:1.12.4" in influxdb
    assert "INFLUXDB_DB: nwn" in influxdb
    assert 'INFLUXDB_UDP_ENABLED: "true"' in influxdb
    assert 'INFLUXDB_UDP_BIND_ADDRESS: ":8089"' in influxdb
    assert "INFLUXDB_UDP_DATABASE: nwn" in influxdb
    assert 'INFLUXDB_REPORTING_DISABLED: "true"' in influxdb
    assert "- profiler_influxdb:/var/lib/influxdb" in influxdb
    assert "ports:" not in influxdb
    assert "image: docker.io/grafana/grafana:13.1.1" in grafana
    assert "      - influxdb" in grafana
    assert 'GF_AUTH_ANONYMOUS_ENABLED: "true"' in grafana
    assert "GF_AUTH_ANONYMOUS_ORG_ROLE: Viewer" in grafana
    assert "GF_SECURITY_ADMIN_USER: admin" in grafana
    assert "GF_SECURITY_ADMIN_PASSWORD: aredev" in grafana
    assert 'GF_USERS_ALLOW_SIGN_UP: "false"' in grafana
    assert '"127.0.0.1:3000:3000"' in grafana
    assert "- profiler_grafana:/var/lib/grafana" in grafana
    assert grafana.count("read_only: true") == 3
    for source, target in (
        (
            "config/grafana/provisioning/datasources",
            "/etc/grafana/provisioning/datasources",
        ),
        (
            "config/grafana/provisioning/dashboards",
            "/etc/grafana/provisioning/dashboards",
        ),
        ("config/grafana/dashboards", "/var/lib/grafana/dashboards"),
    ):
        assert f"source: ${{AREDEV_CONFIG_ROOT:-.}}/{source}" in grafana
        assert f"target: {target}" in grafana
    for dependency in ("db", "grafana", "influxdb"):
        assert f"      - {dependency}" in nwserver
    assert "name: aredev_profiler_influxdb" in compose_text
    assert "name: aredev_profiler_grafana" in compose_text
    assert not compose_text.startswith("name:")

    nwserver_env = _env_values(
        (project / "config" / "nwserver.env").read_text(encoding="utf-8")
    )
    assert nwserver_env["NWNX_METRICS_INFLUXDB_SKIP"] == "no"
    assert nwserver_env["NWNX_METRICS_INFLUXDB_HOST"] == "influxdb"
    assert nwserver_env["NWNX_METRICS_INFLUXDB_PORT"] == "8089"
    for key in (
        "NWNX_PROFILER_ENABLE_OVERHEAD_COMPENSATION",
        "NWNX_PROFILER_ENABLE_AI_MASTER_UPDATES",
        "NWNX_PROFILER_ENABLE_MAIN_LOOP",
        "NWNX_PROFILER_ENABLE_OBJECT_AI_UPDATES",
        "NWNX_PROFILER_ENABLE_OBJECT_EVENT_HANDLERS",
        "NWNX_PROFILER_ENABLE_PATHING",
        "NWNX_PROFILER_ENABLE_SCRIPTS",
        "NWNX_PROFILER_SCRIPTS_AREA_TIMINGS",
        "NWNX_PROFILER_SCRIPTS_TYPE_TIMINGS",
        "NWNX_PROFILER_ENABLE_TICKRATE",
    ):
        assert nwserver_env[key] == "yes"
    assert nwserver_env["NWNX_PROFILER_AI_MASTER_UPDATES_OVERKILL"] == "no"
    assert nwserver_env["NWNX_PROFILER_ENABLE_NET_LAYER"] == "no"
    assert nwserver_env["NWNX_PROFILER_ENABLE_NET_MESSAGES"] == "no"


def test_generated_profiler_compose_configuration_is_valid(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Validate monitoring topology through Docker Compose when it is available."""

    docker = shutil.which("docker")
    if docker is None:
        pytest.skip("Docker CLI is unavailable")
    compose_version = subprocess.run(
        [docker, "compose", "version"],
        text=True,
        capture_output=True,
    )
    if compose_version.returncode != 0:
        pytest.skip("Docker Compose plugin is unavailable")

    _pretend_host_platform(monkeypatch, "linux")
    project = tmp_path / "AREDev"
    initialize_aredev_project(project)
    compose_environment = os.environ.copy()
    compose_environment.update(
        {
            "AREDEV_CONFIG_ROOT": str(project),
            "AREDEV_HOST_ROOT": str(project),
            "AREDEV_NWN_HOME_ROOT": str(project / "server"),
            "AREDEV_NWN_INSTALL_ROOT": str(project),
            "NWN_MODULE": "demo",
        }
    )
    completed = subprocess.run(
        [
            docker,
            "compose",
            "--project-name",
            "aredev-test",
            "--project-directory",
            str(project),
            "--file",
            str(project / "docker-compose.yml"),
            "config",
            "--format",
            "json",
        ],
        cwd=project,
        env=compose_environment,
        text=True,
        capture_output=True,
    )
    assert completed.returncode == 0, completed.stderr

    configuration = json.loads(completed.stdout)
    services = configuration["services"]
    influxdb = services["influxdb"]
    assert influxdb["environment"] == {
        "INFLUXDB_DB": "nwn",
        "INFLUXDB_REPORTING_DISABLED": "true",
        "INFLUXDB_UDP_BIND_ADDRESS": ":8089",
        "INFLUXDB_UDP_DATABASE": "nwn",
        "INFLUXDB_UDP_ENABLED": "true",
    }
    assert influxdb.get("ports", []) == []
    assert {mount["target"]: mount["source"] for mount in influxdb["volumes"]} == {
        "/var/lib/influxdb": "profiler_influxdb"
    }

    grafana = services["grafana"]
    assert grafana["environment"] == {
        "GF_AUTH_ANONYMOUS_ENABLED": "true",
        "GF_AUTH_ANONYMOUS_ORG_ROLE": "Viewer",
        "GF_SECURITY_ADMIN_PASSWORD": "aredev",
        "GF_SECURITY_ADMIN_USER": "admin",
        "GF_USERS_ALLOW_SIGN_UP": "false",
    }
    assert set(grafana["depends_on"]) == {"influxdb"}
    assert len(grafana["ports"]) == 1
    assert {
        key: grafana["ports"][0][key]
        for key in ("host_ip", "target", "published", "protocol")
    } == {
        "host_ip": "127.0.0.1",
        "target": 3000,
        "published": "3000",
        "protocol": "tcp",
    }
    grafana_mounts = {mount["target"]: mount for mount in grafana["volumes"]}
    assert grafana_mounts["/var/lib/grafana"]["source"] == "profiler_grafana"
    for target in (
        "/etc/grafana/provisioning/datasources",
        "/etc/grafana/provisioning/dashboards",
        "/var/lib/grafana/dashboards",
    ):
        assert grafana_mounts[target]["type"] == "bind"
        assert grafana_mounts[target]["read_only"] is True

    assert set(services["nwserver"]["depends_on"]) == {
        "db",
        "grafana",
        "influxdb",
    }
    assert configuration["volumes"]["profiler_influxdb"]["name"] == (
        "aredev_profiler_influxdb"
    )
    assert configuration["volumes"]["profiler_grafana"]["name"] == (
        "aredev_profiler_grafana"
    )


def test_init_generates_windows_batch_wrapper_without_shell_wrapper(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Verify that Windows init generates the batch wrapper instead of the shell wrapper."""

    _pretend_host_platform(monkeypatch, "win32")
    project = tmp_path / "AREDev"

    result = initialize_aredev_project(project)

    assert project / "AREDev.bat" in result.created_files
    assert project / "data" / "bin" / "aredev-host-launcher.ps1" in result.created_files
    assert project / "AREDev.sh" not in result.created_files
    assert (project / "AREDev.bat").is_file()
    assert (project / "data" / "bin" / "aredev-host-launcher.ps1").is_file()
    assert not (project / "AREDev.sh").exists()
    assert not (project / "data" / "bin" / "aredev-host-launcher.sh").exists()

    _assert_uses_crlf_line_endings((project / "AREDev.bat").read_bytes())
    batch_wrapper = (project / "AREDev.bat").read_text(encoding="utf-8")
    powershell_launcher = (
        project / "data" / "bin" / "aredev-host-launcher.ps1"
    ).read_text(encoding="utf-8")
    assert "aredev-host-launcher.ps1" in batch_wrapper
    assert r"%AREDEV_ROOT%\data\bin\aredev-host-launcher.ps1" in batch_wrapper
    assert '-Mode prepare -ProjectRoot "%AREDEV_ROOT%"' in batch_wrapper
    assert '-Mode bridge -ProjectRoot "%AREDEV_ROOT%"' in batch_wrapper
    assert '-Mode update -ProjectRoot "%AREDEV_ROOT%"' in batch_wrapper
    assert "docker compose --progress quiet" in batch_wrapper
    assert "run --rm" in batch_wrapper
    assert "python -m arebuilder aredev" in batch_wrapper
    assert "py -m arebuilder aredev" in batch_wrapper
    assert "WHERE arebuilder" in batch_wrapper
    assert 'arebuilder aredev --root "%AREDEV_ROOT%"' in batch_wrapper
    assert "powershell.exe" in batch_wrapper
    assert "%%~B" in batch_wrapper
    assert "PAUSE" in batch_wrapper
    assert "GOTO EXIT_AREDEV" in batch_wrapper
    for command in [
        "container_status",
        "compose",
        "down_timeout",
        "down_quiet",
        "pull_ignore_failures",
        "volume_drop",
        "update_restart",
    ]:
        assert command in powershell_launcher
    assert "Invoke-Prepare" in powershell_launcher
    assert "Ensure-DockerReady" in powershell_launcher
    assert "DOCKER_EXECUTABLE" in powershell_launcher
    assert "Start-Process -FilePath $dockerStartPath" in powershell_launcher
    assert "Invoke-BridgeLoop" in powershell_launcher
    assert "Invoke-Update" in powershell_launcher
    assert 'ValidateSet("prepare", "bridge", "update")' in powershell_launcher
    assert "--ignore-pull-failures" in powershell_launcher
    assert "docker compose --progress quiet" in powershell_launcher
    assert "down --timeout 1" in powershell_launcher
    assert "-WorkingDirectory (Split-Path -Parent $client)" in powershell_launcher
    assert '$launcherPath = $config["NWN_LAUNCHER_PATH"]' in powershell_launcher


def test_init_generates_posix_shell_launcher_on_macos(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Verify non-Windows hosts generate only the POSIX launcher helpers."""

    _pretend_host_platform(monkeypatch, "darwin")
    project = tmp_path / "AREDev"

    result = initialize_aredev_project(project)

    assert project / "AREDev.sh" in result.created_files
    assert project / "data" / "bin" / "aredev-host-launcher.sh" in result.created_files
    assert (project / "AREDev.sh").is_file()
    assert (project / "data" / "bin" / "aredev-host-launcher.sh").is_file()
    assert not (project / "AREDev.bat").exists()
    assert not (project / "data" / "bin" / "aredev-host-launcher.ps1").exists()
    assert os.access(project / "AREDev.sh", os.X_OK)
    assert os.access(project / "data" / "bin" / "aredev-host-launcher.sh", os.X_OK)


def test_init_is_idempotent_and_preserves_identical_files(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Verify scaffold init can be rerun without rewriting identical existing files."""

    _pretend_host_platform(monkeypatch, "linux")
    project = tmp_path / "AREDev"
    initialize_aredev_project(project)

    result = initialize_aredev_project(project)

    assert project / "AREDev.sh" in result.preserved_files
    assert project / "config" / "arebuilder.env" in result.preserved_files
    assert result.overwritten_files == []


def test_init_refuses_conflicting_files_unless_forced(tmp_path: Path) -> None:
    """Verify scaffold init protects conflicting files unless force mode is enabled."""

    project = tmp_path / "AREDev"
    initialize_aredev_project(project)
    arebuilder_env = project / "config" / "arebuilder.env"
    arebuilder_env.write_text("changed=true\n", encoding="utf-8")

    try:
        initialize_aredev_project(project)
    except ScaffoldConflictError as exc:
        assert "arebuilder.env" in str(exc)
    else:
        raise AssertionError("Expected init to reject a conflicting scaffold file.")

    result = initialize_aredev_project(project, force=True)

    assert arebuilder_env in result.overwritten_files
    assert "BUILD_TARGET=pgcc\n" in arebuilder_env.read_text(encoding="utf-8")


def test_force_init_upgrades_a_pre_monitoring_scaffold(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Verify force init refreshes managed config and adds monitoring assets."""

    _pretend_host_platform(monkeypatch, "linux")
    project = tmp_path / "AREDev"
    initialize_aredev_project(project)

    compose_path = project / "docker-compose.yml"
    nwserver_env_path = project / "config" / "nwserver.env"
    grafana_assets = (
        project
        / "config"
        / "grafana"
        / "provisioning"
        / "datasources"
        / "nwnx.yml",
        project
        / "config"
        / "grafana"
        / "provisioning"
        / "dashboards"
        / "nwnx.yml",
        project
        / "config"
        / "grafana"
        / "dashboards"
        / "nwnx-profiler.json",
    )
    compose_path.write_text("services:\n  nwserver: {}\n", encoding="utf-8")
    nwserver_env_path.write_text("NWNX_CORE_SKIP_ALL=no\n", encoding="utf-8")
    for asset in grafana_assets:
        asset.unlink()

    with pytest.raises(ScaffoldConflictError):
        initialize_aredev_project(project)
    assert all(not asset.exists() for asset in grafana_assets)

    result = initialize_aredev_project(project, force=True)

    assert compose_path in result.overwritten_files
    assert nwserver_env_path in result.overwritten_files
    assert all(asset in result.created_files for asset in grafana_assets)
    assert all(asset.is_file() for asset in grafana_assets)
    assert "  grafana:\n" in compose_path.read_text(encoding="utf-8")
    assert (
        _env_values(nwserver_env_path.read_text(encoding="utf-8"))[
            "NWNX_METRICS_INFLUXDB_SKIP"
        ]
        == "no"
    )


def test_init_cli_and_generated_templates_smoke(tmp_path: Path, monkeypatch) -> None:
    """Verify the init CLI writes templates that can be loaded by project config code."""

    _pretend_host_platform(monkeypatch, "linux")
    project = tmp_path / "AREDev"

    assert main(["init", str(project), "--backend", "docker", "--target", "demo"]) == 0
    arebuilder_env = _env_values(
        (project / "config" / "arebuilder.env").read_text(encoding="utf-8")
    )
    assert arebuilder_env["BUILDER_BACKEND"] == "docker"
    assert arebuilder_env["BUILD_TARGET"] == "demo"
    assert "DOCKER_EXECUTABLE" in arebuilder_env
    compose_text = (project / "docker-compose.yml").read_text(encoding="utf-8")
    shell_wrapper = (project / "AREDev.sh").read_text(encoding="utf-8")
    shell_launcher = (project / "data" / "bin" / "aredev-host-launcher.sh").read_text(
        encoding="utf-8"
    )
    _assert_compose_uses_minimal_builder_mounts(compose_text)
    assert "scaffold_root=" in shell_wrapper
    assert 'exec "$launcher" run "$scaffold_root"' in shell_wrapper
    assert "AREDEV_ROOT=${AREDEV_ROOT:-$scaffold_root}" in shell_wrapper
    assert 'exec aredev --root "$AREDEV_ROOT"' in shell_wrapper
    assert "AREDEV_CONFIG_ROOT=/var/builder" in shell_launcher
    assert "AREDEV_HOST_LAUNCHER=1" in shell_launcher
    assert "DOCKER_EXECUTABLE" in shell_launcher
    assert '"$CONTAINER_CLI" info' in shell_launcher
    assert '--project-directory "$AREDEV_ROOT"' in shell_launcher
    assert "prepare_docker_session" in shell_launcher
    assert "run_host_update" in shell_launcher
    assert "run_update_session" in shell_launcher
    assert "update failed; resuming prompt" in shell_launcher

    shell = shutil.which("sh")
    if shell:
        completed = subprocess.run(
            [shell, "-n", str(project / "AREDev.sh")],
            text=True,
            capture_output=True,
        )
        assert completed.returncode == 0, completed.stderr


def test_init_help_distinguishes_project_dir_from_build_target(capsys) -> None:
    """Verify init help uses unambiguous names for the positional and build target."""

    with pytest.raises(SystemExit) as exc_info:
        main(["init", "--help"])

    assert exc_info.value.code == 0
    output = capsys.readouterr().out
    assert "usage: arebuilder init" in output
    assert "TARGET_DIR" in output
    assert "--target BUILD_TARGET" in output
    assert "AREDev project directory" in output


def test_aredev_cli_without_command_opens_menu_until_exit(
    tmp_path: Path,
    capsys,
    monkeypatch,
) -> None:
    """Verify that aredev CLI without command opens menu until exit."""

    project = tmp_path / "AREDev"
    initialize_aredev_project(project)
    monkeypatch.setattr(
        "arebuilder.app.aredev.controller._create_prompt_session",
        lambda _layout, **_kwargs: _ExitPromptSession(),
    )

    assert main(["aredev", "--root", str(project)]) == 0

    captured = capsys.readouterr()
    assert "AREDev" in captured.out
    assert "Usage:" in captured.out


def test_aredev_cli_defaults_root_from_project_root_env(
    tmp_path: Path,
    capsys,
    monkeypatch,
) -> None:
    """Verify direct aredev CLI launch honors AREDEV_ROOT when --root is omitted."""

    project = tmp_path / "AREDev"
    initialize_aredev_project(project)
    monkeypatch.setenv("AREDEV_ROOT", str(project))
    monkeypatch.setattr(
        "arebuilder.app.aredev.controller._create_prompt_session",
        lambda _layout, **_kwargs: _ExitPromptSession(),
    )

    assert main(["aredev"]) == 0

    captured = capsys.readouterr()
    assert "AREDev" in captured.out
    assert "Usage:" in captured.out
