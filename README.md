# ARE_Builder

![AREDev terminal banner](assets/aredev.png)

`arebuilder` builds Neverwinter Nights: Enhanced Edition module artifacts and compiles NWScript sources. It can compile a standalone script directory, scaffold a local AREDev workspace, run the interactive AREDev workflow, and package module outputs for local development or Docker-backed workflows.

Builder commands use the current directory as the project root by default. In normal use, `cd` into the target AREDev folder first and run commands from there, or specify the project root via `--root [path]`.

## Requirements

- Python 3.13 or newer for native use
- Docker for Docker-backed AREDev projects
- A Neverwinter Nights: Enhanced Edition install for script compilation and server/client workflows
- For AREDev workflows, resource checkouts copied or cloned into the initialized project:
  - `are-resources`
  - `<target>-resources`

## Setup

Choose an empty or existing AREDev project folder, then initialize it with either the native Python package or the published Docker image.

### Native Python

```bash
cd /path/to/AREDev
python -m pip install git+https://github.com/Kalopsia-dev/ARE_Builder.git
arebuilder init .
```

This writes the local AREDev scaffold with native builder execution enabled.

### Docker

```bash
cd /path/to/AREDev
docker pull kalopsiadev/arebuilder:latest
docker run --rm -v ".:/work" -w /work kalopsiadev/arebuilder:latest init /work --backend docker
```

This writes the same scaffold with Docker-backed builder execution enabled. Generated configuration lives in `config/arebuilder.env`; set
`NWN_INSTALL_PATH` and `NWN_HOME_PATH` there if auto-detection cannot find the right locations.

## Daily Workflow

After initialization, use the generated wrapper from the AREDev project root:

```bash
cd /path/to/AREDev
./AREDev.sh
```

On Windows, run `AREDev.bat` instead.

The wrapper opens the interactive AREDev prompt. The same workflows can also run non-interactively from the project root:

```bash
aredev compile
aredev build
aredev start
aredev stop
```

Common AREDev commands are:

| Command | Purpose |
| --- | --- |
| `compile [selector]` | Compile shared scripts, using state to rebuild only what changed unless a selector is provided. |
| `build` | Generate palettes, build the module, and link resources into the server tree. |
| `start` | Start the local AREDev test server with the configured module. |
| `stop` | Stop the AREDev test server. |
| `toolset [run]` | Sync a toolset-compatible module bundle, optionally launching the toolset. |
| `nwn [dm]` | Launch the NWN client and connect to the local server. |
| `database [drop]` | Open the test database client, or reset database state with `drop`. |
| `update` | Update native `arebuilder` from `AREBUILDER_REPO` and pull refreshed Docker images. |

### Profiler Monitoring

`aredev start` also starts InfluxDB and Grafana for local NWNX Profiler
monitoring. Open `http://127.0.0.1:3000` to view the provisioned
**NWNX Profiler** dashboard. Anonymous viewer access is enabled; use `admin` /
`aredev` when administrator access is needed for local dashboard editing.

InfluxDB is available only inside the AREDev Compose network. Profiler telemetry
and Grafana state use their own named volumes, so they persist across
`aredev stop` and are not removed by `database drop`. Detailed Profiler
collection adds instrumentation overhead, so compare performance-sensitive
results with an uninstrumented run before treating them as production baselines.

Existing AREDev projects receive the monitoring scaffold only when deliberately
refreshed with `arebuilder init . --force`. A force refresh overwrites
scaffold-managed configuration, so preserve any local changes first.

## Compile Command

`compile` reads a directory of NWScript sources and writes executable `.ncs` files. By default it stores source hashes so later runs compile only changed scripts and affected dependents. In an AREDev workspace it uses the shared resources tree by default; explicit paths support standalone compilation without an AREDev layout.

```bash
cd /path/to/AREDev
arebuilder compile
arebuilder compile all
arebuilder compile nw_s0_sleep
arebuilder compile 'nw_s0_*'
```

Selector behavior:

- no selector: compile changed shared scripts using the state file
- `all`: compile every shared script with an entry point
- script name or wildcard: compile matching script(s), including dependents when a selected script is an include

Successful incremental stateful compiles also remove outputs for previously
indexed sources that no longer produce executable scripts, and prune obsolete
hash entries. Cleanup includes an enabled live output directory and preserves
unrelated files and precompiled module fallbacks. Named and wildcard compiles
leave pending deletion cleanup for the next incremental run, so deleted includes
still trigger their dependents. Compilation errors leave cleanup unapplied.

Default paths:

| Setting | Default |
| --- | --- |
| input scripts | `are-resources/scripts` |
| compiled output | `compiled-resources` |
| state file | `temp/script_index.json` |

Override those paths with CLI flags when running a standalone compile workflow:

```bash
arebuilder \
  --script-dir /path/to/scripts \
  --output-dir /path/to/compiled \
  --state-file /path/to/script_index.json \
  compile
```

The same settings can be provided through `SCRIPT_DIR`, `OUTPUT_DIR`, and `STATE_FILE`.
Set the game installation with `--nwn-root` or `NWN_ROOT`, and the compiler worker
count with `--workers` or `COMPILE_WORKERS` (by default, CPU count).
If you cannot run from the project directory, use
`--root /path/to/AREDev` or set `AREDEV_ROOT=/path/to/AREDev`.

### TeamCity and CI logging

Pass TeamCity's changed-files list directly for stateless incremental compilation:

```bash
arebuilder \
  --script-dir /path/to/scripts \
  --output-dir /path/to/compiled \
  --nwn-root /path/to/nwn \
  --changes /path/to/changes.txt \
  --ci \
  compile
```

Both `--changes` and `--ci` are CLI-only. `--changes` accepts TeamCity records
with one `relative/path:STATUS:revision` per line, for example:

```text
scripts/example.nss:CHANGED:revision
scripts/new_script.nss:ADDED:revision
scripts/removed_script.nss:REMOVED:revision
```

The first path component must match the configured script directory's name:
`scripts/example.nss` corresponds to `/path/to/scripts/example.nss`. No `--root`
or particular working directory is needed. Only direct `.nss` children are
selected; other directories, non-script files, directory records, and
`NOT_CHANGED` records are ignored. Malformed records fail with a line number.

The complete current script directory is required to discover affected
dependents, including transitive includes. Discovery follows reachable base-game
sources using the same source precedence as compilation: primary scripts, extra
include directories, then game resources. Overrides replace the shipped source's
dependencies; deleting an override restores the shipped dependencies. Game
sources are read in memory and are not stored in the hash index.

`--changes` replaces saved-hash selection: no state file is read, written, or
removed, and unrelated missing
outputs do not trigger compilation. Empty changes succeed without compiling
anything. A positional selector cannot be combined with `--changes`.

`--ci` replaces progress bars and verbose selection details with flushed lines
as each compilation starts. It also works without `--changes`. Parallel workers
may start scripts in different orders. Successful output looks like:

```text
Compiling example.nss
Compiling dependent_script.nss
Compilation complete.

Stale build artifacts:
- removed_script.ncs
```

The optional stale-artifact block is sorted and deduplicated. In `--changes`
mode, these are deletion candidates for the build job to apply; arebuilder
does not clear or delete existing outputs. Changed sources without an entry
point and affected entry-point aliases that are no longer executable also
produce deletion candidates. Removed and changed include-only sources can
therefore produce `.ncs` candidates that never existed; deleting a nonexistent
file must be harmless. Restored files are classified using their current entry
points. An include-directory fallback can keep dependents executable, but does
not preserve the removed primary source's output. Failed runs emit neither the
completion line nor the stale-artifact block.

Treat compiled files as a delta: overlay them on the previous artifact,
preserve unchanged scripts, and apply the explicit deletion candidates.
Absence from this run's output does not mean deletion. The changes list must
cover all changes since that artifact, including changes spanning failed builds.

## Other Builder Commands

Most project work should go through the AREDev wrapper, but the direct
`arebuilder` CLI also exposes lower-level build steps:

| Command | Purpose |
| --- | --- |
| `arebuilder init .` | Create or refresh an AREDev project scaffold. |
| `arebuilder aredev` | Start the AREDev prompt directly. |
| `arebuilder all [target]` | Run the full build and link workflow. |
| `arebuilder quick [target]` | Rebuild fast-changing outputs and repack without regenerating palettes. |
| `arebuilder pack [target]` | Write `module.ifo` and assemble the configured archive. |
| `arebuilder palette [target] [type ...]` | Generate all palettes, or only selected palette types. |
| `arebuilder link [target]` | Create target-specific override links. |
