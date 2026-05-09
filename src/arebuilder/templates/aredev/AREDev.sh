#!/bin/sh
set -eu

# This file is intentionally small. Its job is only to find the AREDev project
# root, read which backend the project wants, and hand off to the real runner.
# Native mode runs the installed Python CLI directly. Docker mode delegates to
# data/bin/aredev-host-launcher.sh, because that path must also work on hosts
# that do not have Python or arebuilder installed.

# $0 is the way POSIX shells expose the script name. It may be a direct path
# like ./AREDev.sh, or just AREDev.sh if the script was found through PATH.
script_path=$0
if [ "${script_path#*/}" = "$script_path" ]; then
    # No slash means the shell found us by name, so ask PATH where the file
    # actually lives. The resolved location lets us find config/ beside it.
    script_path=$(command -v "$script_path") || {
        echo "Unable to locate AREDev.sh on PATH." >&2
        exit 1
    }
fi

# Resolve the directory containing this wrapper. That directory is the AREDev
# project root created by `arebuilder init`.
if ! scaffold_root=$(CDPATH= cd "$(dirname "$script_path")" 2>/dev/null && pwd); then
    echo "Unable to resolve the AREDev project root from: $script_path" >&2
    echo "Launch AREDev with an absolute path or from an existing directory." >&2
    exit 1
fi

CONFIG_FILE=$scaffold_root/config/arebuilder.env
BUILDER_BACKEND=${BUILDER_BACKEND:-native}

strip_surrounding_quotes() {
    # The generated config may contain values such as BUILDER_BACKEND="docker".
    # Shell wrappers only need the bare word for comparison.
    value=$1
    case "$value" in
        \"*\") value=${value#\"}; value=${value%\"} ;;
    esac
    printf '%s\n' "$value"
}

if [ -f "$CONFIG_FILE" ]; then
    # Read only BUILDER_BACKEND here. The host launcher and Python package read
    # the full config later, so this wrapper can stay minimal and predictable.
    while IFS= read -r line || [ -n "$line" ]; do
        case "$line" in
            ""|\#*) continue ;;
            BUILDER_BACKEND=*)
                BUILDER_BACKEND=$(strip_surrounding_quotes "${line#BUILDER_BACKEND=}")
                ;;
        esac
    done < "$CONFIG_FILE"
fi

if [ "$BUILDER_BACKEND" = "docker" ]; then
    # Docker mode must be able to run with Docker plus these generated scripts
    # only. The host launcher owns all host-side setup and the bridge lifecycle.
    launcher=$scaffold_root/data/bin/aredev-host-launcher.sh
    if [ ! -x "$launcher" ]; then
        echo "Host launcher helper not found: $launcher" >&2
        echo "Refresh the AREDev scaffold before using Dockerized AREDev." >&2
        exit 1
    fi
    exec "$launcher" run "$scaffold_root"
fi

# Native mode assumes the user has activated an environment where `aredev`
# is on PATH. Honor AREDEV_ROOT here so host-local environments can choose the
# active project root, while Docker mode still uses the generated scaffold path
# because that is where the helper scripts live.
AREDEV_ROOT=${AREDEV_ROOT:-$scaffold_root}
exec aredev --root "$AREDEV_ROOT"
