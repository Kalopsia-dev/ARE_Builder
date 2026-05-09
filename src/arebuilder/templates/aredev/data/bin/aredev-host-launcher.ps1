param(
    # This script has two modes:
    #
    #   prepare - used by AREDev.bat before foreground Docker starts. It
    #             validates host paths, creates runtime folders, and clears
    #             stale bridge files.
    #
    #   bridge  - used internally while the builder container is running. It
    #             watches temp\host-commands for request files from Python code
    #             inside the container and performs host-only actions.
    #
    #   update  - used by AREDev.bat after the builder requests an image update.
    [ValidateSet("prepare", "bridge", "update")]
    [string]$Mode = "bridge",

    [Parameter(Mandatory = $true)]
    [string]$ProjectRoot,

    [string]$NwnInstallRoot = ""
)

# Stop on normal PowerShell errors. External programs such as docker still
# report status through $LASTEXITCODE, which this script checks explicitly.
$ErrorActionPreference = "Stop"

# Script-scoped variables are shared by functions in this file. They are filled
# during mode setup so lower-level helpers do not need long argument lists.
$script:AredevProject = "aredev"
$script:ProjectRootPath = ""
$script:NwnInstallRootPath = ""
$script:ConfigFile = ""
$script:CommandDir = ""
$script:StopFile = ""
$script:UpdateLockDir = ""

function Resolve-ProjectRoot {
    # Convert the supplied project directory into a full path and fail with a
    # user-oriented message if it cannot be reached.
    param([Parameter(Mandatory = $true)][string]$Path)

    try {
        return (Resolve-Path -LiteralPath $Path).Path
    }
    catch {
        throw "Unable to enter AREDev project root: $Path"
    }
}

function Set-ProjectPaths {
    # All generated support files are addressed relative to the AREDev project
    # root. The command directory is the small file protocol shared with Python.
    param([Parameter(Mandatory = $true)][string]$Root)

    $script:ProjectRootPath = $Root
    $script:ConfigFile = Join-Path $Root "config\arebuilder.env"
    $script:CommandDir = Join-Path $Root "temp\host-commands"
    $script:StopFile = Join-Path $script:CommandDir "stop"
    $script:UpdateLockDir = Join-Path $script:CommandDir "update.lock"
}

function Read-AredevConfig {
    # Read config/arebuilder.env as a simple KEY=value file. This deliberately
    # avoids depending on Python on the host. Unknown keys are preserved in the
    # returned hashtable but most of this script only needs path settings.
    $values = @{}
    if (-not (Test-Path -LiteralPath $script:ConfigFile -PathType Leaf)) {
        return $values
    }

    foreach ($line in [System.IO.File]::ReadLines($script:ConfigFile)) {
        $trimmed = $line.Trim()
        if (-not $trimmed -or $trimmed.StartsWith("#")) {
            continue
        }
        $parts = $trimmed -split "=", 2
        if ($parts.Count -ne 2) {
            continue
        }
        $values[$parts[0]] = Remove-SurroundingQuotes $parts[1]
    }
    return $values
}

function Remove-SurroundingQuotes {
    # The generated config may quote values that contain spaces. For decisions
    # such as backend/path handling we want the value without the outer quotes.
    param([AllowEmptyString()][string]$Value)

    $text = $Value.Trim()
    if ($text.Length -ge 2 -and $text.StartsWith('"') -and $text.EndsWith('"')) {
        return $text.Substring(1, $text.Length - 2)
    }
    return $text
}

function Resolve-AredevPath {
    # Docker bind mounts need host-visible absolute paths. Relative settings are
    # interpreted relative to the AREDev project root; ~ is expanded manually.
    param([Parameter(Mandatory = $true)][string]$Path)

    $expanded = $Path
    if ($expanded -eq "~") {
        $expanded = [Environment]::GetFolderPath("UserProfile")
    }
    elseif ($expanded.StartsWith("~/") -or $expanded.StartsWith("~\")) {
        $expanded = Join-Path ([Environment]::GetFolderPath("UserProfile")) $expanded.Substring(2)
    }
    elseif (-not [System.IO.Path]::IsPathRooted($expanded)) {
        $expanded = Join-Path $script:ProjectRootPath $expanded
    }
    return [System.IO.Path]::GetFullPath($expanded)
}

function Clear-BridgeFiles {
    # Remove stale bridge files before a new builder container starts. Otherwise
    # a request or response from a crashed prior session could be mistaken for a
    # current one.
    New-Item -ItemType Directory -Force -Path $script:CommandDir | Out-Null
    Remove-Item `
        -Force `
        -ErrorAction SilentlyContinue `
        -LiteralPath $script:StopFile
    Remove-Item `
        -Force `
        -ErrorAction SilentlyContinue `
        -Path (Join-Path $script:CommandDir "*.request"), (Join-Path $script:CommandDir "*.response")
}

function Invoke-HostUpdate {
    # The builder container cannot replace its own image. Status 75 asks this
    # host-side process to stop services and pull fresh images instead.
    Write-Host "Updating AREDev containers..."
    & docker compose --progress quiet --project-directory $script:ProjectRootPath --env-file $script:ConfigFile -p $script:AredevProject down
    $downStatus = if ($null -eq $global:LASTEXITCODE) { 0 } else { $global:LASTEXITCODE }
    if ($downStatus -eq 0) {
        & docker compose --progress quiet --project-directory $script:ProjectRootPath --env-file $script:ConfigFile -p $script:AredevProject pull --ignore-pull-failures
        $pullStatus = if ($null -eq $global:LASTEXITCODE) { 0 } else { $global:LASTEXITCODE }
    }
    else {
        $pullStatus = $downStatus
    }
    if ($pullStatus -eq 0) {
        Write-Host "AREDev update complete."
    }
    return $pullStatus
}

function Resolve-DockerRuntimePaths {
    # Resolve Docker host paths from config/arebuilder.env. AREDev.bat keeps the
    # foreground Docker command in cmd.exe for TTY detection, so PowerShell owns
    # path validation and setup instead of emitting another env file.
    $resolvedRoot = Resolve-ProjectRoot $ProjectRoot
    Set-ProjectPaths $resolvedRoot
    $config = Read-AredevConfig

    # Environment values are fallbacks. Non-empty config values win because they
    # are project-local and generated by `arebuilder init`.
    $nwnInstallPath = $env:NWN_INSTALL_PATH
    $nwnHomePath = $env:NWN_HOME_PATH
    if ($config.ContainsKey("NWN_INSTALL_PATH") -and $config["NWN_INSTALL_PATH"]) {
        $nwnInstallPath = $config["NWN_INSTALL_PATH"]
    }
    if ($config.ContainsKey("NWN_HOME_PATH") -and $config["NWN_HOME_PATH"]) {
        $nwnHomePath = $config["NWN_HOME_PATH"]
    }
    if (-not $nwnHomePath) {
        $nwnHomePath = Join-Path $script:ProjectRootPath "server"
    }
    if (-not $nwnInstallPath) {
        [Console]::Error.WriteLine("NWN_INSTALL_PATH is required for Dockerized AREDev.`nSet it in config\arebuilder.env or set it in the environment before starting AREDev.`nIt should point to your host Neverwinter Nights install folder, not the AREDev project.`nUse the folder that contains the game's data and bin directories, such as data\nwn_base.key or bin\win32\nwmain.exe.")
        exit 1
    }
    if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
        [Console]::Error.WriteLine("Unable to find Docker on PATH. Install Docker Desktop or add Docker to PATH before using Dockerized AREDev.")
        exit 1
    }

    $script:NwnInstallRootPath = Resolve-AredevPath $nwnInstallPath
    $nwnHomeRoot = Resolve-AredevPath $nwnHomePath

    return @{
        NwnInstallRoot = $script:NwnInstallRootPath
        NwnHomeRoot = $nwnHomeRoot
    }
}

function Invoke-Prepare {
    # Validate Docker-mode host configuration, create host-side runtime
    # directories, and clear stale bridge files before AREDev.bat launches
    # foreground Docker directly.
    $paths = Resolve-DockerRuntimePaths

    # Compose bind mounts these folders. Creating them here avoids Docker
    # creating unexpected root-owned directories.
    foreach ($name in @("hak", "tlk", "modules")) {
        New-Item -ItemType Directory -Force -Path (Join-Path $paths.NwnHomeRoot $name) | Out-Null
    }

    Clear-BridgeFiles
}

function Invoke-Update {
    # The builder container cannot update the image it is currently running.
    # AREDev.bat invokes this after the bridge accepts an update_restart request.
    $resolvedRoot = Resolve-ProjectRoot $ProjectRoot
    Set-ProjectPaths $resolvedRoot
    try {
        $status = Invoke-HostUpdate
    }
    finally {
        Remove-Item -LiteralPath $script:UpdateLockDir -Force -ErrorAction SilentlyContinue
    }
    exit $status
}

function Convert-ToResponseLine {
    # Response files are one field per line, with key and value separated by a
    # tab. Newlines and tabs in command output would corrupt that simple format,
    # so collapse them into spaces before writing a response.
    param([AllowEmptyString()][string]$Value)

    # Response files use one tab-delimited record per field, so collapse control
    # characters that would otherwise create extra rows or columns.
    return $Value -replace "[`r`n`t]", " "
}

function Read-RequestFile {
    # Parse one request file produced by Python in the builder container. Unknown
    # keys are allowed so newer builders can add fields without breaking older
    # host launchers.
    param([Parameter(Mandatory = $true)][string]$Path)

    $values = @{}
    foreach ($line in [System.IO.File]::ReadLines($Path)) {
        $parts = $line -split "`t", 2
        if ($parts.Count -eq 2) {
            $values[$parts[0]] = $parts[1]
        }
    }
    return $values
}

function Write-LauncherResponse {
    # Write the response to a temporary file first, then move it into place.
    # That move is effectively atomic for this workflow, so the container never
    # observes a partially-written response.
    param(
        [Parameter(Mandatory = $true)][string]$ResponsePath,
        [Parameter(Mandatory = $true)][string]$Status,
        [Parameter(Mandatory = $true)][int]$ReturnCode,
        [AllowEmptyString()][string]$Stdout = "",
        [AllowEmptyString()][string]$Stderr = "",
        [AllowEmptyString()][string]$Message = ""
    )

    $tempPath = "$ResponsePath.tmp.$PID"
    $lines = @(
        "STATUS`t$Status",
        "RETURN_CODE`t$ReturnCode",
        "STDOUT`t$(Convert-ToResponseLine $Stdout)",
        "STDERR`t$(Convert-ToResponseLine $Stderr)",
        "MESSAGE`t$(Convert-ToResponseLine $Message)"
    )
    # Write to a temp file and move it into place so the builder never observes a
    # partially-written response.
    [System.IO.File]::WriteAllLines(
        $tempPath,
        $lines,
        [System.Text.UTF8Encoding]::new($false)
    )
    Move-Item -Force -LiteralPath $tempPath -Destination $ResponsePath
}

function Find-NwnClient {
    # Locate the NWN client executable on Windows. This is used by the `nwn`
    # command to launch the local client and connect to the test server.
    param([Parameter(Mandatory = $true)][string]$InstallRoot)

    $win32Dir = Join-Path $InstallRoot "bin\win32"
    $candidates = @(
        (Join-Path $win32Dir "nwmain.exe"),
        (Join-Path $win32Dir "nwmain")
    )
    foreach ($candidate in $candidates) {
        if (Test-Path -LiteralPath $candidate -PathType Leaf) {
            return $candidate
        }
    }

    $matches = Get-ChildItem `
        -LiteralPath $win32Dir `
        -Filter "nwmain*" `
        -File `
        -ErrorAction SilentlyContinue |
        Sort-Object -Property FullName
    if ($matches) {
        # Some installs add suffixes to nwmain; sorted fallback keeps selection
        # deterministic when exact names are absent.
        return $matches[0].FullName
    }
    return $null
}

function Find-NwnToolset {
    # The Toolset is expected at the classic Windows NWN install location.
    # Keeping this check narrow gives a direct error when the install path is
    # not the folder AREDev expects.
    param([Parameter(Mandatory = $true)][string]$InstallRoot)

    $candidate = Join-Path $InstallRoot "bin\win32\nwtoolset.exe"
    if (Test-Path -LiteralPath $candidate -PathType Leaf) {
        return $candidate
    }
    return $null
}

function Quote-ProcessArgument {
    # Start-Process -ArgumentList is easiest to use as a single string in a few
    # places below. Quote values containing spaces or quotes so paths survive
    # the round trip through another process.
    param([AllowEmptyString()][string]$Value)

    if ($Value -notmatch '[\s"]') {
        return $Value
    }
    $escaped = $Value -replace '"', '\"'
    return '"' + $escaped + '"'
}

function Invoke-CapturedCommand {
    # Run a host command and capture both output and exit status. Docker failures
    # are returned to Python as response files instead of throwing PowerShell
    # errors that would stop the bridge loop.
    param(
        [Parameter(Mandatory = $true)][scriptblock]$Command
    )

    try {
        $output = & $Command 2>&1 | Out-String
        return @{
            ExitCode = if ($null -eq $global:LASTEXITCODE) { 0 } else { $global:LASTEXITCODE }
            Output = $output
        }
    }
    catch {
        return @{
            ExitCode = 1
            Output = $_.Exception.Message
        }
    }
}

function Invoke-ComposeAction {
    # Python inside the builder container sends symbolic Compose actions here.
    # This function translates those symbols to the exact host Docker commands.
    param(
        [Parameter(Mandatory = $true)][string]$ResponsePath,
        [Parameter(Mandatory = $true)][hashtable]$Values
    )

    $action = $Values["ACTION"]
    $previousModule = $env:NWN_MODULE
    if ($Values.ContainsKey("NWN_MODULE")) {
        # NWN_MODULE is transient per Compose request and must be restored after
        # the command to avoid leaking state into the host shell process.
        $env:NWN_MODULE = $Values["NWN_MODULE"]
    }

    if ($action -eq "up_nwserver") {
        $result = Invoke-CapturedCommand {
            docker compose --progress quiet --project-directory $script:ProjectRootPath --env-file $script:ConfigFile -p $script:AredevProject up -d nwserver
        }
    }
    elseif ($action -eq "down") {
        $result = Invoke-CapturedCommand {
            docker compose --progress quiet --project-directory $script:ProjectRootPath --env-file $script:ConfigFile -p $script:AredevProject down
        }
    }
    elseif ($action -eq "down_quiet") {
        $result = Invoke-CapturedCommand {
            docker compose --progress quiet --project-directory $script:ProjectRootPath --env-file $script:ConfigFile -p $script:AredevProject down
        }
    }
    elseif ($action -eq "pull") {
        $result = Invoke-CapturedCommand {
            docker compose --progress quiet --project-directory $script:ProjectRootPath --env-file $script:ConfigFile -p $script:AredevProject pull
        }
    }
    elseif ($action -eq "pull_ignore_failures") {
        $result = Invoke-CapturedCommand {
            docker compose --progress quiet --project-directory $script:ProjectRootPath --env-file $script:ConfigFile -p $script:AredevProject pull --ignore-pull-failures
        }
    }
    else {
        Write-LauncherResponse `
            -ResponsePath $ResponsePath `
            -Status "error" `
            -ReturnCode 1 `
            -Stderr "Unsupported host Compose action: $action" `
            -Message "Unsupported host Compose action: $action"
        return
    }

    if ($null -eq $previousModule) {
        Remove-Item Env:\NWN_MODULE -ErrorAction SilentlyContinue
    }
    else {
        $env:NWN_MODULE = $previousModule
    }

    if ($result.ExitCode -eq 0) {
        Write-LauncherResponse `
            -ResponsePath $ResponsePath `
            -Status "ok" `
            -ReturnCode 0 `
            -Stdout $result.Output
    }
    else {
        Write-LauncherResponse `
            -ResponsePath $ResponsePath `
            -Status "error" `
            -ReturnCode $result.ExitCode `
            -Stderr $result.Output `
            -Message $result.Output
    }
}

function Test-ContainerStatus {
    # Return whether a named Docker container is running. Python uses this for
    # start/stop/database/nwn decisions.
    param(
        [Parameter(Mandatory = $true)][string]$ResponsePath,
        [Parameter(Mandatory = $true)][hashtable]$Values
    )

    $result = Invoke-CapturedCommand { docker ps --format "{{.Names}}" }
    if ($result.ExitCode -ne 0) {
        Write-LauncherResponse `
            -ResponsePath $ResponsePath `
            -Status "error" `
            -ReturnCode $result.ExitCode `
            -Stderr $result.Output `
            -Message $result.Output
        return
    }

    $containerName = $Values["CONTAINER"]
    $found = "false"
    foreach ($name in ($result.Output -split "`r?`n")) {
        if ($name -eq $containerName) {
            $found = "true"
            break
        }
    }
    Write-LauncherResponse `
        -ResponsePath $ResponsePath `
        -Status "ok" `
        -ReturnCode 0 `
        -Stdout $found
}

function Remove-DatabaseVolumes {
    # Drop AREDev's MariaDB data volumes from the host. This cannot happen from
    # inside the isolated builder container, so the bridge owns it.
    param([Parameter(Mandatory = $true)][string]$ResponsePath)

    $result = Invoke-CapturedCommand { docker volume ls --format "{{.Name}}" }
    if ($result.ExitCode -ne 0) {
        Write-LauncherResponse `
            -ResponsePath $ResponsePath `
            -Status "error" `
            -ReturnCode $result.ExitCode `
            -Stderr $result.Output `
            -Message $result.Output
        return
    }

    $existing = @()
    foreach ($volume in @("aredev_data", "aredev_database")) {
        if (($result.Output -split "`r?`n") -contains $volume) {
            $existing += $volume
        }
    }
    if ($existing.Count -eq 0) {
        Write-LauncherResponse `
            -ResponsePath $ResponsePath `
            -Status "ok" `
            -ReturnCode 0 `
            -Stdout "Database volume not found."
        return
    }

    $removeResult = Invoke-CapturedCommand { docker volume rm @existing }
    if ($removeResult.ExitCode -eq 0) {
        Write-LauncherResponse `
            -ResponsePath $ResponsePath `
            -Status "ok" `
            -ReturnCode 0 `
            -Stdout "Database dropped."
    }
    else {
        Write-LauncherResponse `
            -ResponsePath $ResponsePath `
            -Status "error" `
            -ReturnCode $removeResult.ExitCode `
            -Stderr $removeResult.Output `
            -Message $removeResult.Output
    }
}

function Accept-UpdateRestart {
    # The Python controller requests this when the user runs `update` inside the
    # builder container. Creating a directory is used as an atomic lock so two
    # AREDev sessions cannot both update images at once.
    param([Parameter(Mandatory = $true)][string]$ResponsePath)

    try {
        New-Item -ItemType Directory -Path $script:UpdateLockDir -ErrorAction Stop | Out-Null
        Write-LauncherResponse -ResponsePath $ResponsePath -Status "ok" -ReturnCode 0
    }
    catch {
        Write-LauncherResponse `
            -ResponsePath $ResponsePath `
            -Status "error" `
            -ReturnCode 1 `
            -Stderr "Another AREDev update is already running." `
            -Message "Another AREDev update is already running."
    }
}

function Invoke-NwnRequest {
    # Launch the local NWN client and connect it to the published server port.
    # The client process is intentionally detached from AREDev.
    param(
        [Parameter(Mandatory = $true)][string]$ResponsePath,
        [Parameter(Mandatory = $true)][hashtable]$Values
    )

    $modeValue = if ($Values.ContainsKey("MODE")) { $Values["MODE"] } else { "player" }
    $port = if ($Values.ContainsKey("PORT")) { $Values["PORT"] } else { "5121" }
    $password = if ($Values.ContainsKey("PASSWORD")) { $Values["PASSWORD"] } else { "aredev" }

    $client = Find-NwnClient -InstallRoot $script:NwnInstallRootPath
    if (-not $client) {
        Write-LauncherResponse `
            -ResponsePath $ResponsePath `
            -Status "error" `
            -ReturnCode 1 `
            -Stderr "Unable to find the NWN client executable under $($script:NwnInstallRootPath)." `
            -Message "Unable to find the NWN client executable under $($script:NwnInstallRootPath)."
        return
    }

    $arguments = @()
    if ($modeValue -ieq "dm") {
        $arguments += "-dmc"
    }
    $arguments += @("+connect", "127.0.0.1:$port", "+password", $password)
    $argumentLine = ($arguments | ForEach-Object { Quote-ProcessArgument $_ }) -join " "
    Start-Process `
        -FilePath $client `
        -WorkingDirectory (Split-Path -Parent $client) `
        -ArgumentList $argumentLine | Out-Null
    Write-LauncherResponse -ResponsePath $ResponsePath -Status "ok" -ReturnCode 0
}

function Test-ToolsetRequest {
    # Check whether the Toolset can be launched before Python spends time
    # preparing the Toolset bundle.
    param([Parameter(Mandatory = $true)][string]$ResponsePath)

    $toolset = Find-NwnToolset -InstallRoot $script:NwnInstallRootPath
    if (-not $toolset) {
        Write-LauncherResponse `
            -ResponsePath $ResponsePath `
            -Status "error" `
            -ReturnCode 1 `
            -Stderr "Unable to find the NWN Toolset executable under $($script:NwnInstallRootPath)." `
            -Message "Unable to find the NWN Toolset executable under $($script:NwnInstallRootPath)."
        return
    }

    Write-LauncherResponse -ResponsePath $ResponsePath -Status "ok" -ReturnCode 0
}

function Invoke-ToolsetRequest {
    # Launch the NWN Toolset from the host. AREDev starts it and then returns to
    # the prompt; it does not wait for the Toolset to close.
    param([Parameter(Mandatory = $true)][string]$ResponsePath)

    $toolset = Find-NwnToolset -InstallRoot $script:NwnInstallRootPath
    if (-not $toolset) {
        Write-LauncherResponse `
            -ResponsePath $ResponsePath `
            -Status "error" `
            -ReturnCode 1 `
            -Stderr "Unable to find the NWN Toolset executable under $($script:NwnInstallRootPath)." `
            -Message "Unable to find the NWN Toolset executable under $($script:NwnInstallRootPath)."
        return
    }

    Start-Process `
        -FilePath $toolset `
        -WorkingDirectory (Split-Path -Parent $toolset) | Out-Null
    Write-LauncherResponse -ResponsePath $ResponsePath -Status "ok" -ReturnCode 0
}

function Invoke-HostRequest {
    # Dispatch one request file from the builder container. The response path has
    # the same base name as the request path, but with .response as the suffix.
    param([Parameter(Mandatory = $true)][System.IO.FileInfo]$Request)

    $responsePath = [System.IO.Path]::ChangeExtension($Request.FullName, ".response")
    try {
        $values = Read-RequestFile -Path $Request.FullName
        $command = $values["COMMAND"]
        # Keep the command switch explicit so unsupported builder requests fail
        # with a protocol error instead of silently doing nothing.
        if ($command -eq "nwn") {
            Invoke-NwnRequest -ResponsePath $responsePath -Values $values
        }
        elseif ($command -eq "toolset_check") {
            Test-ToolsetRequest -ResponsePath $responsePath
        }
        elseif ($command -eq "toolset") {
            Invoke-ToolsetRequest -ResponsePath $responsePath
        }
        elseif ($command -eq "container_status") {
            Test-ContainerStatus -ResponsePath $responsePath -Values $values
        }
        elseif ($command -eq "compose") {
            Invoke-ComposeAction -ResponsePath $responsePath -Values $values
        }
        elseif ($command -eq "volume_drop") {
            Remove-DatabaseVolumes -ResponsePath $responsePath
        }
        elseif ($command -eq "update_restart") {
            Accept-UpdateRestart -ResponsePath $responsePath
        }
        else {
            Write-LauncherResponse `
                -ResponsePath $responsePath `
                -Status "error" `
                -ReturnCode 1 `
                -Stderr "Unsupported AREDev host command: $command" `
                -Message "Unsupported AREDev host command: $command"
        }
    }
    catch {
        Write-LauncherResponse `
            -ResponsePath $responsePath `
            -Status "error" `
            -ReturnCode 1 `
            -Stderr $_.Exception.Message `
            -Message $_.Exception.Message
    }
    finally {
        Remove-Item -Force -LiteralPath $Request.FullName -ErrorAction SilentlyContinue
    }
}

function Invoke-BridgeLoop {
    # Background mode. Poll for request files until AREDev.bat writes the stop
    # file. Requests are processed serially so Docker and NWN launch side effects
    # happen in the same order Python asked for them.
    $resolvedRoot = Resolve-ProjectRoot $ProjectRoot
    Set-ProjectPaths $resolvedRoot
    if (-not $NwnInstallRoot) {
        $paths = Resolve-DockerRuntimePaths
        $NwnInstallRoot = $paths.NwnInstallRoot
    }
    $script:NwnInstallRootPath = $NwnInstallRoot
    New-Item -ItemType Directory -Force -Path $script:CommandDir | Out-Null

    while ($true) {
        if (Test-Path -LiteralPath $script:StopFile -PathType Leaf) {
            Remove-Item -Force -LiteralPath $script:StopFile
            exit 0
        }

        # Process requests serially to keep Docker and client-launch side effects in
        # the same order the builder produced them.
        Get-ChildItem `
            -LiteralPath $script:CommandDir `
            -Filter "*.request" `
            -File `
            -ErrorAction SilentlyContinue |
            ForEach-Object { Invoke-HostRequest -Request $_ }

        Start-Sleep -Milliseconds 250
    }
}

try {
    # Dispatch based on the requested operating mode. Any unexpected exception
    # is printed as a plain stderr line so batch callers get a readable failure.
    if ($Mode -ieq "prepare") {
        Invoke-Prepare
    }
    elseif ($Mode -ieq "update") {
        Invoke-Update
    }
    else {
        Invoke-BridgeLoop
    }
}
catch {
    [Console]::Error.WriteLine($_.Exception.Message)
    exit 1
}
