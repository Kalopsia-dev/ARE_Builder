#!/bin/sh
set -eu

# This script has four modes:
#
#   run     - used by AREDev.sh in Docker mode. It runs on the host, calls
#             prepare, starts the bridge loop, launches the builder container,
#             and handles container-image updates.
#
#   prepare - validates Docker-mode configuration and creates host folders that
#             are used as bind mounts by the builder container.
#
#   bridge  - used internally while the builder container is running. It watches
#             temp/host-commands for request files written by Python code inside
#             the container, performs host-only work, and writes response files.
#
#   update  - performs the host-side container-image update after the builder
#             exits with the internal restart status.
#
# Keeping these jobs in a POSIX shell script means Docker mode only needs Docker
# plus the generated scaffold files on the host. Native Python is not required.

AREDEV_PROJECT=aredev
RESTART_EXIT_CODE=75
TAB=$(printf '\t')

# These variables are filled in by set_project_paths and by run/bridge mode
# setup. They are global because POSIX shell functions communicate most simply
# through variables like these.
AREDEV_ROOT=
NWN_INSTALL_ROOT=
AREDEV_NWN_HOME_ROOT=
CONFIG_FILE=
COMMAND_DIR=
STOP_FILE=
UPDATE_LOCK_DIR=
HOST_LAUNCHER_PID=
OWNS_UPDATE_LOCK=0

usage() {
    echo "Usage:" >&2
    echo "  aredev-host-launcher.sh run <project-root>" >&2
    echo "  aredev-host-launcher.sh prepare <project-root>" >&2
    echo "  aredev-host-launcher.sh bridge <project-root> [nwn-install-root]" >&2
    echo "  aredev-host-launcher.sh update <project-root>" >&2
    exit 2
}

set_project_paths() {
    # All generated support files live under the project root. The command
    # directory is a tiny file-based protocol shared with the Python package.
    AREDEV_ROOT=$1
    CONFIG_FILE=$AREDEV_ROOT/config/arebuilder.env
    COMMAND_DIR=$AREDEV_ROOT/temp/host-commands
    STOP_FILE=$COMMAND_DIR/stop
    UPDATE_LOCK_DIR=$COMMAND_DIR/update.lock
}

resolve_project_root() {
    # Convert the user-provided path into an absolute physical directory path.
    # CDPATH= prevents the user's shell CDPATH setting from changing output.
    if ! root=$(CDPATH= cd "$1" 2>/dev/null && pwd); then
        echo "Unable to enter AREDev project root: $1" >&2
        exit 1
    fi
    printf '%s\n' "$root"
}

load_aredev_config() {
    # Preserve values that were exported before AREDev started. A blank setting
    # in config/arebuilder.env should not erase a host-specific environment
    # override for the NWN install/home paths.
    ENV_NWN_INSTALL_PATH=${NWN_INSTALL_PATH:-}
    ENV_NWN_HOME_PATH=${NWN_HOME_PATH:-}

    if [ -f "$CONFIG_FILE" ]; then
        # shellcheck source=/dev/null - generated config is a simple KEY=value file.
        set -a
        . "$CONFIG_FILE"
        set +a
    fi

    NWN_INSTALL_PATH=${NWN_INSTALL_PATH:-$ENV_NWN_INSTALL_PATH}
    NWN_HOME_PATH=${NWN_HOME_PATH:-$ENV_NWN_HOME_PATH}
    NWN_HOME_PATH=${NWN_HOME_PATH:-$AREDEV_ROOT/server}
}

resolve_aredev_path() {
    # Docker bind mounts require host-visible paths. Relative project settings
    # are interpreted relative to the AREDev project root; "~" is expanded here
    # because docker compose does not expand it for us.
    path=$1
    case "$path" in
        "~") path=$HOME ;;
        "~/"*) path=$HOME/${path#~/} ;;
        /*) ;;
        *) path=$AREDEV_ROOT/$path ;;
    esac
    if [ -d "$path" ]; then
        (CDPATH= cd "$path" && pwd)
    else
        printf '%s\n' "$path"
    fi
}

clear_bridge_files() {
    mkdir -p "$COMMAND_DIR"
    # Remove stale requests before starting a new builder container so an older
    # interrupted session cannot receive or satisfy current commands.
    rm -f "$COMMAND_DIR"/stop "$COMMAND_DIR"/*.request "$COMMAND_DIR"/*.response
}

resolve_docker_runtime_paths() {
    load_aredev_config

    if [ -z "${NWN_INSTALL_PATH:-}" ]; then
        echo "NWN_INSTALL_PATH is required for Dockerized AREDev." >&2
        echo "Set it in config/arebuilder.env or export it before starting AREDev." >&2
        echo "It should point to your host Neverwinter Nights install folder, not the AREDev project." >&2
        echo "Use the folder that contains the game's data/ and bin/ directories, such as data/nwn_base.key or bin/.../nwmain." >&2
        exit 1
    fi
    if ! command -v docker >/dev/null 2>&1; then
        echo "Unable to find Docker on PATH. Install Docker before using Dockerized AREDev." >&2
        exit 1
    fi

    NWN_INSTALL_ROOT=$(resolve_aredev_path "$NWN_INSTALL_PATH")
    AREDEV_NWN_HOME_ROOT=$(resolve_aredev_path "$NWN_HOME_PATH")

    # These values are consumed by docker-compose.yml and also preserved inside
    # the builder container as host-path metadata for bridge operations.
    export AREDEV_HOST_ROOT=$AREDEV_ROOT
    export AREDEV_CONFIG_ROOT=$AREDEV_ROOT
    export AREDEV_NWN_INSTALL_ROOT=$NWN_INSTALL_ROOT
    export AREDEV_NWN_HOME_ROOT
}

prepare_docker_session() {
    if [ "$#" -ne 1 ]; then
        usage
    fi

    project_root=$(resolve_project_root "$1")
    set_project_paths "$project_root"
    resolve_docker_runtime_paths

    # Docker compose bind mounts these folders. Creating them up front gives a
    # clearer failure point and avoids Docker creating root-owned directories.
    mkdir -p "$AREDEV_NWN_HOME_ROOT/hak" "$AREDEV_NWN_HOME_ROOT/tlk" "$AREDEV_NWN_HOME_ROOT/modules"
    clear_bridge_files
}

start_bridge_loop() {
    # Launch a second copy of this same script in bridge mode. It runs in the
    # background while the builder container is alive, so containerized Python
    # can ask the host to start NWN, run Docker commands, or launch the Toolset.
    launcher=$0
    if [ "${launcher#*/}" = "$launcher" ]; then
        launcher=$(command -v "$launcher") || {
            echo "Unable to locate AREDev host launcher on PATH." >&2
            exit 1
        }
    fi

    "$launcher" bridge "$AREDEV_ROOT" &
    HOST_LAUNCHER_PID=$!
}

stop_bridge_loop() {
    # The bridge loop exits when it sees the stop file. Waiting here prevents a
    # later AREDev session from racing with a still-running old bridge process.
    if [ -n "${HOST_LAUNCHER_PID:-}" ]; then
        mkdir -p "$COMMAND_DIR"
        : > "$STOP_FILE"
        wait "$HOST_LAUNCHER_PID" 2>/dev/null || true
        HOST_LAUNCHER_PID=
    fi
}

cleanup_update_lock() {
    # The Python controller creates this lock by requesting update_restart from
    # the bridge. The run-mode host process owns cleanup after it finishes
    # pulling container images.
    if [ "$OWNS_UPDATE_LOCK" = "1" ]; then
        rmdir "$UPDATE_LOCK_DIR" 2>/dev/null || true
        OWNS_UPDATE_LOCK=0
    fi
}

run_host_update() {
    # The builder container cannot update the images it is currently running
    # from. Returning status 75 asks this host process to stop containers and
    # pull updated images, then optionally start a fresh interactive prompt.
    echo "Updating AREDev containers..."
    set +e
    docker compose --progress quiet --project-directory "$AREDEV_ROOT" --env-file "$CONFIG_FILE" -p "$AREDEV_PROJECT" down
    down_status=$?
    if [ "$down_status" -eq 0 ]; then
        docker compose --progress quiet --project-directory "$AREDEV_ROOT" --env-file "$CONFIG_FILE" -p "$AREDEV_PROJECT" pull --ignore-pull-failures
        pull_status=$?
    else
        pull_status=$down_status
    fi
    if [ "$pull_status" -eq 0 ]; then
        echo "AREDev update complete."
    fi
    return "$pull_status"
}

run_docker_session() {
    # This is the main Docker-mode entry point called by AREDev.sh. It keeps the
    # wrapper compact by owning preparation, bridge startup, foreground Docker,
    # and update restarts in one host-side place.
    if [ "$#" -ne 1 ]; then
        usage
    fi

    project_root=$(resolve_project_root "$1")
    set_project_paths "$project_root"

    # Always stop the background bridge and clean our update lock on exit, even
    # if the user presses Ctrl+C.
    trap 'stop_bridge_loop; cleanup_update_lock' EXIT INT TERM

    while :; do
        prepare_docker_session "$AREDEV_ROOT"
        start_bridge_loop
        set +e
        docker compose --progress quiet --project-directory "$AREDEV_ROOT" --env-file "$CONFIG_FILE" -p "$AREDEV_PROJECT" run --rm \
            -e "AREDEV_HOST_ROOT=$AREDEV_ROOT" \
            -e "AREDEV_CONFIG_ROOT=/var/builder" \
            -e "AREDEV_NWN_INSTALL_ROOT=$NWN_INSTALL_ROOT" \
            -e "AREDEV_NWN_HOME_ROOT=$AREDEV_NWN_HOME_ROOT" \
            -e "AREDEV_HOST_LAUNCHER=1" \
            builder aredev --root /var/builder
        status=$?
        set -e
        stop_bridge_loop

        # Status 75 is an internal "please restart after update" signal from
        # Python, not a user-facing error.
        if [ "$status" -eq "$RESTART_EXIT_CODE" ]; then
            OWNS_UPDATE_LOCK=1
            set +e
            run_host_update
            update_status=$?
            set -e
            cleanup_update_lock
            if [ "$update_status" -ne 0 ]; then
                echo "AREDev update failed; resuming prompt with existing images." >&2
                continue
            fi
            continue
        fi
        exit "$status"
    done
}

run_update_session() {
    if [ "$#" -ne 1 ]; then
        usage
    fi

    project_root=$(resolve_project_root "$1")
    set_project_paths "$project_root"
    resolve_docker_runtime_paths
    OWNS_UPDATE_LOCK=1

    set +e
    run_host_update
    status=$?
    set -e
    cleanup_update_lock
    return "$status"
}

one_line() {
    # Response files are tab-delimited single-line values, so collapse control
    # characters that would otherwise corrupt the protocol.
    printf '%s' "$1" | tr '\n\r\t' '   '
}

write_response() {
    # Responses are tiny tab-delimited files. We write to a temp file first and
    # rename it into place so the builder container never reads half a response.
    response_path=$1
    status=$2
    return_code=$3
    stdout_text=$4
    stderr_text=$5
    message=$6
    temp_path=$response_path.tmp.$$
    {
        printf 'STATUS\t%s\n' "$status"
        printf 'RETURN_CODE\t%s\n' "$return_code"
        printf 'STDOUT\t%s\n' "$(one_line "$stdout_text")"
        printf 'STDERR\t%s\n' "$(one_line "$stderr_text")"
        printf 'MESSAGE\t%s\n' "$(one_line "$message")"
    } > "$temp_path"
    # Atomic rename prevents the builder container from reading a partial file.
    mv "$temp_path" "$response_path"
}

write_ok() {
    # Convenience wrapper for successful bridge responses.
    write_response "$1" ok 0 "${2:-}" "" "${3:-}"
}

write_error() {
    # Convenience wrapper for failed bridge responses. The same text is written
    # to stderr and message so old and new callers both receive useful context.
    write_response "$1" error "${3:-1}" "" "$2" "$2"
}

find_nwn_client() {
    # Locate the NWN client executable for the host OS. This is used only by the
    # `nwn` command, where Python inside the container asks the host to launch a
    # local game client and connect it to the server.
    install_root=$1
    system=$(uname -s 2>/dev/null || printf 'Linux')
    machine=$(uname -m 2>/dev/null || printf 'x86_64')

    if [ "$system" = "Darwin" ]; then
        # macOS packages may expose nwmain either inside the app bundle or as a
        # direct binary under bin/macos.
        for candidate in \
            "$install_root/bin/macos/nwmain.app/Contents/MacOS/nwmain" \
            "$install_root/bin/macos/nwmain"
        do
            if [ -f "$candidate" ]; then
                printf '%s\n' "$candidate"
                return 0
            fi
        done
        return 1
    fi

    variants="linux-x86 linux-arm64"
    case "$machine" in
        arm64|aarch64) variants="linux-arm64 linux-x86" ;;
    esac

    # Prefer the architecture-specific directory, but try both Linux layouts
    # because some installs include compatibility binaries.
    for variant in $variants; do
        for candidate in \
            "$install_root/bin/$variant/nwmain" \
            "$install_root/bin/$variant/nwmain.exe" \
            "$install_root/bin/$variant"/nwmain*
        do
            if [ -f "$candidate" ]; then
                printf '%s\n' "$candidate"
                return 0
            fi
        done
    done
    return 1
}

find_nwn_toolset() {
    # The Toolset is a Windows executable even when launched from Linux/macOS
    # through wine. The check is deliberately narrow so failures are obvious.
    install_root=$1
    candidate=$install_root/bin/win32/nwtoolset.exe
    if [ -f "$candidate" ]; then
        printf '%s\n' "$candidate"
        return 0
    fi
    return 1
}

find_wine() {
    # On non-Windows hosts, the NWN Toolset requires wine. Prefer wine64 when it
    # exists but accept wine as a fallback.
    if command -v wine64 >/dev/null 2>&1; then
        command -v wine64
        return 0
    fi
    if command -v wine >/dev/null 2>&1; then
        command -v wine
        return 0
    fi
    return 1
}

check_toolset() {
    # Validate that a later `toolset run` request has the pieces it needs. The
    # Python controller performs this check before preparing the Toolset bundle.
    response_path=$1

    if ! toolset=$(find_nwn_toolset "$NWN_INSTALL_ROOT"); then
        write_error "$response_path" "Unable to find the NWN Toolset executable under $NWN_INSTALL_ROOT."
        return
    fi
    if ! wine=$(find_wine); then
        write_error "$response_path" "The NWN Toolset is unavailable on this platform and wine was not found on PATH."
        return
    fi
    # Keep shellcheck quiet about the intentionally validated values.
    : "$toolset" "$wine"
    write_ok "$response_path"
}

launch_toolset() {
    # Start the NWN Toolset on the host. It is launched in the background so the
    # AREDev prompt can continue rather than waiting for the Toolset to exit.
    response_path=$1

    if ! toolset=$(find_nwn_toolset "$NWN_INSTALL_ROOT"); then
        write_error "$response_path" "Unable to find the NWN Toolset executable under $NWN_INSTALL_ROOT."
        return
    fi
    if ! wine=$(find_wine); then
        write_error "$response_path" "The NWN Toolset is unavailable on this platform and wine was not found on PATH."
        return
    fi

    toolset_dir=$(dirname "$toolset")
    (cd "$toolset_dir" && "$wine" start /unix "$toolset") >/dev/null 2>&1 &
    write_ok "$response_path"
}

run_compose_action() {
    # The builder container has no Docker socket. When Python needs to start or
    # stop services, it sends a small symbolic action here and this host script
    # translates that action into the exact Compose command.
    response_path=$1
    action=$2
    nwn_module=$3

    set +e
    case "$action" in
        up_nwserver)
            # NWN_MODULE is only needed by the server startup action.
            output=$(NWN_MODULE=$nwn_module docker compose --progress quiet --project-directory "$AREDEV_ROOT" --env-file "$CONFIG_FILE" -p "$AREDEV_PROJECT" up -d nwserver 2>&1)
            status=$?
            ;;
        down)
            output=$(docker compose --progress quiet --project-directory "$AREDEV_ROOT" --env-file "$CONFIG_FILE" -p "$AREDEV_PROJECT" down 2>&1)
            status=$?
            ;;
        down_quiet)
            output=$(docker compose --progress quiet --project-directory "$AREDEV_ROOT" --env-file "$CONFIG_FILE" -p "$AREDEV_PROJECT" down 2>&1)
            status=$?
            ;;
        pull)
            output=$(docker compose --progress quiet --project-directory "$AREDEV_ROOT" --env-file "$CONFIG_FILE" -p "$AREDEV_PROJECT" pull 2>&1)
            status=$?
            ;;
        pull_ignore_failures)
            output=$(docker compose --progress quiet --project-directory "$AREDEV_ROOT" --env-file "$CONFIG_FILE" -p "$AREDEV_PROJECT" pull --ignore-pull-failures 2>&1)
            status=$?
            ;;
        *)
            set -e
            write_error "$response_path" "Unsupported host Compose action: $action"
            return
            ;;
    esac
    set -e

    if [ "$status" -eq 0 ]; then
        write_response "$response_path" ok 0 "$output" "" ""
    else
        write_response "$response_path" error "$status" "" "$output" "$output"
    fi
}

check_container_status() {
    # Return whether a named Docker container is currently running. Python uses
    # this to decide whether commands like start/stop/database/nwn make sense.
    response_path=$1
    container_name=$2

    set +e
    names=$(docker ps --format "{{.Names}}" 2>&1)
    status=$?
    set -e
    if [ "$status" -ne 0 ]; then
        write_response "$response_path" error "$status" "" "$names" "$names"
        return
    fi

    found=false
    old_ifs=$IFS
    IFS='
'
    for name in $names; do
        # docker ps output is newline-delimited; compare exact names to avoid
        # matching similarly prefixed containers.
        if [ "$name" = "$container_name" ]; then
            found=true
            break
        fi
    done
    IFS=$old_ifs
    write_ok "$response_path" "$found"
}

drop_database_volumes() {
    # Remove the AREDev database volumes from the host. This stays outside the
    # container for the same reason Compose commands do: only the host controls
    # Docker state.
    response_path=$1

    set +e
    volumes=$(docker volume ls --format "{{.Name}}" 2>&1)
    status=$?
    set -e
    if [ "$status" -ne 0 ]; then
        write_response "$response_path" error "$status" "" "$volumes" "$volumes"
        return
    fi

    existing=
    for volume in aredev_data aredev_database; do
        old_ifs=$IFS
        IFS='
'
        for name in $volumes; do
            if [ "$name" = "$volume" ]; then
                existing="$existing $volume"
            fi
        done
        IFS=$old_ifs
    done

    if [ -z "$existing" ]; then
        write_ok "$response_path" "Database volume not found."
        return
    fi

    set +e
    output=$(docker volume rm $existing 2>&1)
    status=$?
    set -e
    if [ "$status" -eq 0 ]; then
        write_ok "$response_path" "Database dropped."
    else
        write_response "$response_path" error "$status" "" "$output" "$output"
    fi
}

accept_update_restart() {
    # Python calls this when the user asks for `update` inside the builder
    # container. Creating a directory is an atomic lock operation on normal
    # filesystems, so competing AREDev sessions cannot both own the update.
    response_path=$1
    if mkdir "$UPDATE_LOCK_DIR" 2>/dev/null; then
        write_ok "$response_path"
    else
        write_error "$response_path" "Another AREDev update is already running."
    fi
}

launch_nwn() {
    # Start the NWN client on the host and connect it to the local test server.
    # The client is deliberately backgrounded; AREDev only needs to initiate it.
    response_path=$1
    mode=$2
    port=$3
    password=$4

    if client=$(find_nwn_client "$NWN_INSTALL_ROOT"); then
        client_dir=$(dirname "$client")
        if [ "$mode" = "dm" ]; then
            (cd "$client_dir" && "$client" -dmc +connect "127.0.0.1:$port" +password "$password") >/dev/null 2>&1 &
        else
            (cd "$client_dir" && "$client" +connect "127.0.0.1:$port" +password "$password") >/dev/null 2>&1 &
        fi
        write_ok "$response_path"
    else
        write_error "$response_path" "Unable to find the NWN client executable under $NWN_INSTALL_ROOT."
    fi
}

process_request() {
    # Read one request file, dispatch it to a host action, and delete the
    # request after a response has been written. The request/response shape is:
    #
    #   KEY<TAB>VALUE
    #
    # One request file maps to one response file with the same base name.
    request_path=$1
    response_path=${request_path%.request}.response
    command=
    mode=player
    port=5121
    password=aredev
    container_name=
    action=
    nwn_module=

    while IFS="$TAB" read -r key value || [ -n "${key:-}" ]; do
        # Unknown keys are ignored so newer builders can add fields without
        # breaking older host launchers.
        case "$key" in
            COMMAND) command=$value ;;
            MODE) mode=$value ;;
            PORT) port=$value ;;
            PASSWORD) password=$value ;;
            CONTAINER) container_name=$value ;;
            ACTION) action=$value ;;
            NWN_MODULE) nwn_module=$value ;;
        esac
    done < "$request_path"

    case "$command" in
        nwn) launch_nwn "$response_path" "$mode" "$port" "$password" ;;
        toolset_check) check_toolset "$response_path" ;;
        toolset) launch_toolset "$response_path" ;;
        container_status) check_container_status "$response_path" "$container_name" ;;
        compose) run_compose_action "$response_path" "$action" "$nwn_module" ;;
        volume_drop) drop_database_volumes "$response_path" ;;
        update_restart) accept_update_restart "$response_path" ;;
        *) write_error "$response_path" "Unsupported AREDev host command: $command" ;;
    esac

    rm -f "$request_path"
}

run_bridge_loop() {
    # Background loop used while the builder container is running. It wakes up
    # once per second, processes any request files, and exits when run mode
    # writes the stop file.
    if [ "$#" -lt 1 ] || [ "$#" -gt 2 ]; then
        usage
    fi

    project_root=$(resolve_project_root "$1")
    set_project_paths "$project_root"
    if [ "$#" -eq 2 ]; then
        NWN_INSTALL_ROOT=$2
    else
        resolve_docker_runtime_paths
    fi
    mkdir -p "$COMMAND_DIR"

    trap 'exit 0' INT TERM

    while :; do
        if [ -f "$STOP_FILE" ]; then
            rm -f "$STOP_FILE"
            exit 0
        fi

        for request_path in "$COMMAND_DIR"/*.request; do
            if [ -f "$request_path" ]; then
                # Requests are processed serially to keep Docker and NWN launch side
                # effects ordered and easy to reason about.
                process_request "$request_path"
            fi
        done

        sleep 1
    done
}

if [ "$#" -lt 1 ]; then
    usage
fi

mode=$1
shift
# First argument selects the operating mode. Everything after it belongs to
# that mode's entry point.
case "$mode" in
    run) run_docker_session "$@" ;;
    prepare) prepare_docker_session "$@" ;;
    bridge) run_bridge_loop "$@" ;;
    update) run_update_session "$@" ;;
    *) usage ;;
esac
