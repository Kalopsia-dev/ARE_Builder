@ECHO off
SETLOCAL EnableExtensions

REM Windows interactive AREDev launcher. Use `arebuilder aredev --root ...`
REM directly for one-shot commands.

REM Batch syntax is intentionally plain here because this file is generated into
REM user projects. Most Docker-mode host logic lives in the PowerShell helper;
REM this wrapper only chooses the backend and keeps the foreground Docker TTY
REM owned by cmd.exe.

REM %~dp0 is the directory that contains this .bat file. Appending "." makes the
REM value usable as a project-root path without caring whether it ends in "\".
SET "AREDEV_ROOT=%~dp0."
SET "CONFIG_FILE=%AREDEV_ROOT%\config\arebuilder.env"
SET "HOST_LAUNCHER=%AREDEV_ROOT%\data\bin\aredev-host-launcher.ps1"
SET "HOST_COMMAND_DIR=%AREDEV_ROOT%\temp\host-commands"

SET "AREDEV_STATUS=0"
SET "AREDEV_WAIT_ON_ERROR=0"
REM When the user double-clicks the launcher, cmd.exe would otherwise close on
REM errors before they can read the message. Command-line launches skip PAUSE.
IF "%~1"=="" SET "AREDEV_WAIT_ON_ERROR=1"

SET "BUILDER_BACKEND=native"
SET "RESTART_EXIT_CODE=75"

REM Read only BUILDER_BACKEND here. The Python and PowerShell entry points read
REM the complete config later, so this small batch parser can stay predictable.
IF EXIST "%CONFIG_FILE%" FOR /F "usebackq eol=# tokens=1,* delims==" %%A IN ("%CONFIG_FILE%") DO IF /I "%%A"=="BUILDER_BACKEND" SET "BUILDER_BACKEND=%%~B"
CALL :STRIP_SURROUNDING_QUOTES BUILDER_BACKEND
IF /I "%BUILDER_BACKEND%"=="docker" GOTO DOCKER_AREDEV

:NATIVE_AREDEV
REM Do not call bare `aredev` from AREDev.bat. Windows command lookup is
REM case-insensitive, so `aredev` can resolve back to AREDev.bat and recurse.
WHERE arebuilder >NUL 2>NUL
IF ERRORLEVEL 1 GOTO TRY_NATIVE_PY
arebuilder aredev --root "%AREDEV_ROOT%"
SET "AREDEV_STATUS=%ERRORLEVEL%"
GOTO EXIT_AREDEV

:TRY_NATIVE_PY
REM Fallback for environments where the console script is missing but the Python
REM launcher can still import the package.
WHERE py >NUL 2>NUL
IF ERRORLEVEL 1 GOTO NATIVE_AREDEV_MISSING
py -m arebuilder aredev --root "%AREDEV_ROOT%"
SET "AREDEV_STATUS=%ERRORLEVEL%"
GOTO EXIT_AREDEV

:NATIVE_AREDEV_MISSING
ECHO Unable to find the arebuilder command on PATH. 1>&2
ECHO Activate the Python environment that has arebuilder installed, or set BUILDER_BACKEND=docker in config\arebuilder.env. 1>&2
SET "AREDEV_STATUS=1"
GOTO EXIT_AREDEV

:DOCKER_AREDEV
IF EXIST "%HOST_LAUNCHER%" GOTO HOST_LAUNCHER_FOUND
ECHO Host launcher helper not found: %HOST_LAUNCHER% 1>&2
ECHO Refresh the AREDev scaffold before using Dockerized AREDev. 1>&2
SET "AREDEV_STATUS=1"
GOTO EXIT_AREDEV

:HOST_LAUNCHER_FOUND
REM Docker mode still needs PowerShell for host-only work such as validation,
REM host-command bridging, and container-image updates.
WHERE powershell.exe >NUL 2>NUL
IF ERRORLEVEL 1 GOTO POWERSHELL_MISSING

REM These environment variables are consumed by docker-compose.yml. They point
REM Compose at the generated project files on the Windows host.
SET "AREDEV_HOST_ROOT=%AREDEV_ROOT%"
SET "AREDEV_CONFIG_ROOT=%AREDEV_ROOT%"

:DOCKER_AREDEV_LOOP
REM `prepare` validates config and creates bind-mount directories before Docker
REM starts. It does not create temporary Compose env files.
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%HOST_LAUNCHER%" -Mode prepare -ProjectRoot "%AREDEV_ROOT%"
SET "AREDEV_STATUS=%ERRORLEVEL%"
IF NOT "%AREDEV_STATUS%"=="0" GOTO EXIT_AREDEV

REM The bridge watches temp\host-commands while the container is alive. Python
REM code inside Docker uses that tiny file protocol to ask the host to run NWN,
REM the Toolset, or Docker Compose commands.
START "" /B powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%HOST_LAUNCHER%" -Mode bridge -ProjectRoot "%AREDEV_ROOT%"

REM Keep the foreground `docker compose run` in cmd.exe. Launching this path
REM through PowerShell on Windows can confuse Compose TTY detection.
docker compose --progress quiet --project-directory "%AREDEV_ROOT%" --env-file "%CONFIG_FILE%" -p aredev run --rm -e "AREDEV_HOST_ROOT=%AREDEV_ROOT%" -e "AREDEV_CONFIG_ROOT=/var/builder" -e "AREDEV_HOST_LAUNCHER=1" builder aredev --root /var/builder
SET "AREDEV_STATUS=%ERRORLEVEL%"

REM Tell the background bridge to exit, then either restart after an update or
REM return the Docker exit status to the caller.
COPY /Y NUL "%HOST_COMMAND_DIR%\stop" >NUL
IF "%AREDEV_STATUS%"=="%RESTART_EXIT_CODE%" GOTO UPDATE_AND_RESTART
GOTO EXIT_AREDEV

:UPDATE_AND_RESTART
REM Status 75 is AREDev's internal "pull updated images, then reopen the prompt"
REM signal. If update fails, keep the existing images usable.
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%HOST_LAUNCHER%" -Mode update -ProjectRoot "%AREDEV_ROOT%"
IF ERRORLEVEL 1 ECHO AREDev update failed; resuming prompt with existing images. 1>&2
GOTO DOCKER_AREDEV_LOOP

:POWERSHELL_MISSING
ECHO Unable to find powershell.exe on PATH. PowerShell is required for Dockerized AREDev. 1>&2
SET "AREDEV_STATUS=1"
GOTO EXIT_AREDEV

:STRIP_SURROUNDING_QUOTES
REM Config files may use BUILDER_BACKEND="docker". Batch variable expansion does
REM not have a clean trim helper, so this tiny subroutine removes all quotes
REM from the named variable.
CALL SET "AREDEV_VALUE=%%%~1%%"
SET "AREDEV_VALUE=%AREDEV_VALUE:"=%"
SET "%~1=%AREDEV_VALUE%"
EXIT /B 0

:EXIT_AREDEV
IF "%AREDEV_STATUS%"=="" SET "AREDEV_STATUS=0"
REM Only pause for double-click/error launches. Successful command-line runs
REM should exit quietly with their process status.
IF NOT "%AREDEV_STATUS%"=="0" IF "%AREDEV_WAIT_ON_ERROR%"=="1" (
    ECHO.
    ECHO AREDev exited with status %AREDEV_STATUS%.
    PAUSE
)
EXIT /B %AREDEV_STATUS%
