#requires -version 5.1
<#
.SYNOPSIS
    agent-voice installer for Windows.

.DESCRIPTION
    EXPERIMENTAL / NOT VERIFIED ON REAL WINDOWS.

    This script mirrors scripts/install.sh for Windows. It was written against
    Windows PowerShell 5.1 and the .NET Framework, kept ASCII-only, and may only
    have been parse-checked in this release; it has NOT been run on a real
    Windows host. Treat it as experimental until the release notes say
    otherwise. Report problems at https://github.com/000jd/agent-voice/issues.

    What it does, in order:
      1. resolves the release to install: the latest release tag by default,
         never `main` (`main` only with an explicit -Ref main), -Version wins;
      2. installs uv with the official installer (https://astral.sh/uv/install.ps1)
         only when uv is missing, asking first unless -Yes -- never elevated;
      3. clones (or updates) the checkout into -Dir;
      4. `uv sync` the root project;
      5. writes an absolute <install>\.env (AGENT_VOICE_HOME, DATA_DIR,
         ENGINES_DIR), backing up any existing .env first;
      6. writes the <install>\bin\agent-voice.cmd shim;
      7. runs `agent-voice setup` with the forwarded flags (unless -NoSetup).

    The data directory is %LOCALAPPDATA%\agent-voice-data, outside the install
    directory, and -Uninstall never removes it.

    Security: this fetches code from the network and executes it. Read it first,
    and use -DryRun to audit the exact plan. It never requests elevation and
    never sends telemetry.

.PARAMETER Dir
    Where to install the checkout. Default: %LOCALAPPDATA%\agent-voice

.PARAMETER Version
    Release tag to install, e.g. v0.6.0. Wins over the resolved latest tag.

.PARAMETER Ref
    Install a branch or commit instead of a release tag, e.g. main.

.PARAMETER Repo
    Repository to clone. A local path works offline.
    Default: https://github.com/000jd/agent-voice.git

.PARAMETER Yes
    Never prompt. Accepts installing uv and passes --yes to `agent-voice setup`.
    Does not download model weights.

.PARAMETER Profile
    Hardware profile forwarded to `agent-voice setup`:
    auto | cpu-only | nvidia-gpu | apple-silicon

.PARAMETER Download
    Forward --download to `agent-voice setup`.

.PARAMETER NoDownload
    Forward --no-download to `agent-voice setup`.

.PARAMETER NoSetup
    Stop after the install and the shim; do not run `agent-voice setup`.

.PARAMETER Uninstall
    Remove the shim and the install directory. The data directory is NEVER
    removed; its path is printed.

.PARAMETER DryRun
    Print every action and every argv, execute nothing, write nothing.

.PARAMETER Help
    Print this help and exit 0.

.EXAMPLE
    powershell -ExecutionPolicy ByPass -File install.ps1 -Yes

.EXAMPLE
    .\install.ps1 -Version v0.6.0 -Profile nvidia-gpu -Download -Yes

.EXAMPLE
    .\install.ps1 -Repo C:\src\agent-voice -Dir C:\tmp\av -NoSetup -DryRun

.EXAMPLE
    .\install.ps1 -Uninstall
#>
[CmdletBinding()]
param(
    [string]$Dir = '',
    [string]$Version = '',
    [string]$Ref = '',
    [string]$Repo = 'https://github.com/000jd/agent-voice.git',
    [switch]$Yes,
    [string]$Profile = '',
    [switch]$Download,
    [switch]$NoDownload,
    [switch]$NoSetup,
    [switch]$Uninstall,
    [switch]$DryRun,
    [switch]$Help
)

$ErrorActionPreference = 'Stop'

$Prog = 'agent-voice-install'
$UvInstallerUrl = 'https://astral.sh/uv/install.ps1'

# --- Output -----------------------------------------------------------------
function Write-Say {
    param([string]$Message)
    Write-Host $Message
}

function Write-Warn {
    param([string]$Message)
    Write-Host "$Prog`: warning: $Message" -ForegroundColor Yellow
}

function Fail {
    param([string]$Message)
    Write-Host "$Prog`: error: $Message" -ForegroundColor Red
    exit 1
}

function Show-Usage {
    Write-Say @'
agent-voice installer for Windows -- EXPERIMENTAL / NOT VERIFIED ON REAL WINDOWS.

Usage:
  powershell -ExecutionPolicy ByPass -File install.ps1 [options]

Options:
  -Dir <path>        Where to install the checkout.
                     Default: %LOCALAPPDATA%\agent-voice
  -Version <tag>     Release tag to install, e.g. v0.6.0. Wins over the
                     resolved latest tag.
  -Ref <branch|sha>  Install a branch or commit instead of a release tag,
                     e.g. -Ref main. Unpinned; prefer -Version.
  -Repo <url|path>   Repository to clone. A local path works offline.
                     Default: https://github.com/000jd/agent-voice.git
  -Yes               Never prompt. Accepts installing uv and passes --yes to
                     `agent-voice setup`. Does not download model weights.
  -Profile <name>    Hardware profile forwarded to `agent-voice setup`:
                     auto (default) | cpu-only | nvidia-gpu | apple-silicon
  -Download          Forward --download to `agent-voice setup`.
  -NoDownload        Forward --no-download to `agent-voice setup`.
  -NoSetup           Stop after the install and the shim.
  -Uninstall         Remove the shim and the install directory. The data
                     directory is NEVER removed; its path is printed.
  -DryRun            Print every action and every argv, execute nothing,
                     write nothing. This is the audit path.
  -Help              Print this help and exit 0.

Environment:
  LOCALAPPDATA       Required. Defaults for the install and data directories
                     are derived from it.
  AGENT_VOICE_HOME   If already set, the installer warns: it writes
                     AGENT_VOICE_HOME into <install>\.env and the shim sets it.

Examples:
  .\install.ps1 -Yes
  .\install.ps1 -Version v0.6.0 -Profile nvidia-gpu -Download -Yes
  .\install.ps1 -Dir C:\src\agent-voice -NoSetup
  .\install.ps1 -Repo C:\src\agent-voice -DryRun
  .\install.ps1 -Uninstall

Notes:
  - The installer never requests elevation.
  - uv is only installed when missing, and only after consent (or -Yes), from
    https://astral.sh/uv/install.ps1.
  - The data directory defaults to %LOCALAPPDATA%\agent-voice-data.
  - EXPERIMENTAL: this script is not verified on a real Windows host in 0.6.0.
'@
}

# --- Small helpers ----------------------------------------------------------
function Format-Argument {
    param([string]$Value)
    if ($Value -match '^[A-Za-z0-9_./:=@%+\\-]+$') { return $Value }
    return '"' + ($Value -replace '"', '\"') + '"'
}

function Format-Argv {
    param([string]$File, [string[]]$Arguments)
    $parts = @($File)
    if ($Arguments) { $parts += $Arguments }
    $formatted = @()
    foreach ($part in $parts) { $formatted += (Format-Argument $part) }
    return ($formatted -join ' ')
}

# Invoke-Action -File <exe> -Arguments <argv>: execute it, or print it in -DryRun.
# Native output goes to the host, never to the caller's pipeline, so functions
# such as Resolve-Uv keep a clean return value.
function Invoke-Action {
    param([string]$File, [string[]]$Arguments = @())
    $line = '+ ' + (Format-Argv $File $Arguments)
    if ($DryRun) {
        Write-Say $line
        return
    }
    Write-Say $line
    & $File @Arguments | Out-Host
    if ($LASTEXITCODE -ne 0) {
        Fail "$File failed with exit code $LASTEXITCODE"
    }
}

function Test-Command {
    param([string]$Name)
    $cmd = Get-Command $Name -ErrorAction SilentlyContinue
    if ($null -eq $cmd) { return $null }
    return $cmd
}

function Expand-HomePath {
    param([string]$Path)
    $home = $env:USERPROFILE
    if ([string]::IsNullOrEmpty($home)) { $home = $env:HOME }
    if ([string]::IsNullOrEmpty($Path)) { return $Path }
    if ($Path -eq '~') { return $home }
    if ($Path.StartsWith('~\') -or $Path.StartsWith('~/')) {
        return (Join-Path $home $Path.Substring(2))
    }
    return $Path
}

function Resolve-FullPath {
    param([string]$Path)
    $expanded = Expand-HomePath $Path
    return [System.IO.Path]::GetFullPath($expanded)
}

function Ensure-Directory {
    param([string]$Path)
    if ($DryRun) {
        Write-Say "+ New-Item -ItemType Directory -Force -Path $Path"
        return
    }
    if (-not (Test-Path -LiteralPath $Path)) {
        New-Item -ItemType Directory -Force -Path $Path | Out-Null
    }
}

function Confirm-Action {
    param([string]$Question)
    if ($Yes) { return $true }
    $answer = Read-Host "$Question [y/N]"
    if ($answer -match '^(y|Y|yes|YES|Yes)$') { return $true }
    return $false
}

# --- Release resolution -----------------------------------------------------
# Latest release tag = highest vX.Y.Z from `git ls-remote --tags --refs`,
# sorted as [version]. Never falls back to main: an empty answer is a hard error.
function Get-LatestTag {
    param([string]$RepoUrl)
    $lines = & git ls-remote --tags --refs $RepoUrl 2>$null
    if ($LASTEXITCODE -ne 0) { return $null }
    $tags = @()
    foreach ($line in @($lines)) {
        if ($line -match 'refs/tags/(v\d+\.\d+\.\d+)$') {
            $tags += $Matches[1]
        }
    }
    if ($tags.Count -eq 0) { return $null }
    $sorted = $tags | Sort-Object -Property { [version]($_.Substring(1)) } -Descending
    return @($sorted)[0]
}

# --- uv ---------------------------------------------------------------------
function Resolve-Uv {
    $cmd = Test-Command 'uv'
    if ($null -ne $cmd) {
        Write-Say "uv: $($cmd.Source)"
        return $cmd.Source
    }

    Write-Say 'uv is not installed; it is required to sync the project.'
    Write-Say "The official installer would be run: $UvInstallerUrl"
    Write-Say 'It installs uv under your user profile (no elevation).'

    if ($DryRun) {
        Write-Say '+ powershell -NoProfile -ExecutionPolicy ByPass -Command "irm https://astral.sh/uv/install.ps1 | iex"'
        return (Join-Path $ProfileHome '.local\bin\uv.exe')
    }

    if (-not (Confirm-Action 'Install uv now?')) {
        Fail 'uv is required; install it from https://docs.astral.sh/uv/ or re-run with -Yes'
    }

    Invoke-Action -File 'powershell' -Arguments @(
        '-NoProfile',
        '-ExecutionPolicy', 'ByPass',
        '-Command', "irm $UvInstallerUrl | iex"
    )

    # The official installer is known to land in one of these; add them to this
    # process so the rest of the script works without a new shell.
    $candidates = @(
        (Join-Path $ProfileHome '.local\bin'),
        (Join-Path $env:LOCALAPPDATA 'uv'),
        (Join-Path $ProfileHome '.cargo\bin')
    )
    foreach ($candidate in $candidates) {
        if (Test-Path -LiteralPath $candidate) {
            if (($env:PATH -split ';') -notcontains $candidate) {
                $env:PATH = $candidate + ';' + $env:PATH
            }
        }
    }

    $cmd = Test-Command 'uv'
    if ($null -eq $cmd) {
        Fail 'the uv installer finished but uv is not on PATH; open a new shell and re-run'
    }
    Write-Say "uv: $($cmd.Source)"
    return $cmd.Source
}

# --- Checkout ---------------------------------------------------------------
function Assert-Git {
    $cmd = Test-Command 'git'
    if ($null -ne $cmd) { return $cmd.Source }
    if ($DryRun) {
        Write-Warn 'git was not found on PATH; a real run would fail here'
        return 'git'
    }
    Fail 'git is required; install git and re-run'
}

function Test-Checkout {
    # Read-only: refuse a bad install dir before uv is installed or a clone runs.
    if (-not (Test-Path -LiteralPath $InstallDir)) { return }
    if (-not (Test-Path -LiteralPath (Join-Path $InstallDir '.git'))) {
        Fail "install directory exists but is not a git checkout: $InstallDir (move it away or pick another -Dir)"
    }
    $dirty = @(& git -C $InstallDir status --porcelain --untracked-files=no 2>$null)
    if ($LASTEXITCODE -ne 0) {
        Fail "could not read the git status of $InstallDir"
    }
    if ($dirty.Count -gt 0) {
        Fail "install directory has local changes to tracked files: $InstallDir (commit/stash them or pick another -Dir)"
    }
}

function Invoke-Checkout {
    if ($DryRun) {
        Write-Say "+ git -C $InstallDir checkout --detach $Target"
        return
    }
    & git -C $InstallDir checkout --detach $Target 2>$null | Out-Null
    if ($LASTEXITCODE -eq 0) {
        Write-Say "checked out $Target"
        return
    }
    # A branch fetched from a remote lives under refs/remotes/origin/<name>.
    & git -C $InstallDir checkout --detach "origin/$Target" 2>$null | Out-Null
    if ($LASTEXITCODE -eq 0) {
        Write-Say "checked out origin/$Target"
        return
    }
    Fail "could not check out '$Target' from $Repo"
}

function Sync-Checkout {
    param([string]$Git)

    if (Test-Path -LiteralPath $InstallDir) {
        Test-Checkout
        Write-Say "Updating existing checkout at $InstallDir"
        # Explicit refspecs: independent of the checkout's origin configuration.
        Invoke-Action -File $Git -Arguments @(
            '-C', $InstallDir, 'fetch', '--tags', '--force', $Repo,
            '+refs/heads/*:refs/remotes/origin/*'
        )
        Invoke-Checkout
        return
    }

    Write-Say "Cloning $Repo into $InstallDir"
    Ensure-Directory (Split-Path -Parent $InstallDir)
    Invoke-Action -File $Git -Arguments @('clone', $Repo, $InstallDir)
    Invoke-Checkout
}

# --- .env -------------------------------------------------------------------
function Backup-Env {
    if (-not (Test-Path -LiteralPath $EnvFile)) { return }
    $stamp = (Get-Date).ToUniversalTime().ToString('yyyyMMddTHHmmssZ')
    $existingBak = "$EnvFile.bak"
    if (Test-Path -LiteralPath $existingBak) {
        # Never overwrite an existing .env.bak: it may be the only copy.
        $backup = "$EnvFile.$stamp.bak"
        $n = 1
        while (Test-Path -LiteralPath $backup) {
            $backup = "$EnvFile.$stamp-$n.bak"
            $n = $n + 1
        }
    }
    else {
        $backup = $existingBak
    }
    Write-Say "Backing up existing .env: $EnvFile -> $backup"
    if ($DryRun) {
        Write-Say "+ copy $EnvFile $backup"
        return
    }
    Copy-Item -LiteralPath $EnvFile -Destination $backup
}

function Write-EnvFile {
    Backup-Env

    $managed = '^(AGENT_VOICE_HOME|DATA_DIR|ENGINES_DIR)='
    if ($DryRun) {
        Write-Say "+ write $EnvFile"
        Write-Say "    AGENT_VOICE_HOME=$InstallDir"
        Write-Say "    DATA_DIR=$DataDir"
        Write-Say "    ENGINES_DIR=$EnginesDir"
        return
    }

    $lines = @()
    if (Test-Path -LiteralPath $EnvFile) {
        # Keep every other line (comments, unknown keys) untouched.
        foreach ($line in [System.IO.File]::ReadAllLines($EnvFile)) {
            if ($line -notmatch $managed) { $lines += $line }
        }
    }
    else {
        $lines += '# agent-voice configuration written by scripts/install.ps1.'
        $lines += '# Paths are absolute so the CLI works from any directory.'
    }
    $lines += ''
    $lines += '# --- Installer paths (absolute, managed by scripts/install.ps1) ---'
    $lines += "AGENT_VOICE_HOME=$InstallDir"
    $lines += "DATA_DIR=$DataDir"
    $lines += "ENGINES_DIR=$EnginesDir"

    [System.IO.File]::WriteAllText($EnvFile, (($lines -join "`r`n") + "`r`n"), [System.Text.Encoding]::ASCII)
    Write-Say "Wrote $EnvFile"
}

# --- Shim -------------------------------------------------------------------
function Write-Shim {
    Ensure-Directory $ShimDir

    $content = @(
        '@echo off',
        'rem Generated by agent-voice scripts/install.ps1 -- edits are overwritten.',
        'set "AGENT_VOICE_HOME=' + $InstallDir + '"',
        '"' + (Join-Path $InstallDir '.venv\Scripts\agent-voice.exe') + '" %*',
        'exit /b %ERRORLEVEL%'
    ) -join "`r`n"

    if ($DryRun) {
        Write-Say "+ write $Shim (sets AGENT_VOICE_HOME=$InstallDir; runs $InstallDir\.venv\Scripts\agent-voice.exe %*)"
        return
    }
    [System.IO.File]::WriteAllText($Shim, ($content + "`r`n"), [System.Text.Encoding]::ASCII)
    Write-Say "Wrote $Shim"
}

function Test-PathEntry {
    $entries = ($env:PATH -split ';')
    foreach ($entry in $entries) {
        if ($entry.TrimEnd('\') -eq $ShimDir.TrimEnd('\')) { return $true }
    }
    return $false
}

function Write-PathWarning {
    if (-not (Test-PathEntry)) {
        Write-Warn "$ShimDir is not on PATH; add it, e.g.:  setx PATH `"%PATH%;$ShimDir`""
    }
}

# --- uninstall --------------------------------------------------------------
function Invoke-Uninstall {
    Write-Say 'Uninstalling agent-voice'
    Write-Say "  shim:    $Shim"
    Write-Say "  install: $InstallDir"
    Write-Say "  data:    $DataDir (kept)"

    $root = [System.IO.Path]::GetPathRoot($InstallDir)
    if ($InstallDir.TrimEnd('\') -eq $root.TrimEnd('\')) {
        Fail "refusing to remove '$InstallDir'"
    }
    if ($InstallDir.TrimEnd('\') -eq $ProfileHome.TrimEnd('\')) {
        Fail "refusing to remove '$InstallDir'"
    }

    if (Test-Path -LiteralPath $Shim) {
        if ($DryRun) { Write-Say "+ Remove-Item -Force $Shim" }
        else { Remove-Item -LiteralPath $Shim -Force }
    }
    else {
        Write-Say '  shim not present, nothing to remove'
    }

    if (Test-Path -LiteralPath $InstallDir) {
        if ($DryRun) { Write-Say "+ Remove-Item -Recurse -Force $InstallDir" }
        else { Remove-Item -LiteralPath $InstallDir -Recurse -Force }
    }
    else {
        Write-Say '  install directory not present, nothing to remove'
    }

    if ($DryRun) { Write-Say 'Dry run: nothing was removed.' }
    Write-Say "Data directory kept: $DataDir"
    Write-Say "To delete it yourself: Remove-Item -Recurse -Force `"$DataDir`""
}

# --- Main -------------------------------------------------------------------
if ($Help) {
    Show-Usage
    exit 0
}

if ($Download -and $NoDownload) {
    Fail '-Download and -NoDownload are mutually exclusive'
}
if (($Version -ne '') -and ($Ref -ne '')) {
    Fail '-Version and -Ref are mutually exclusive: a release tag or a branch/commit'
}
if ($Profile -ne '') {
    $allowed = @('auto', 'cpu-only', 'nvidia-gpu', 'apple-silicon')
    if ($allowed -notcontains $Profile) {
        Fail "-Profile must be one of: auto, cpu-only, nvidia-gpu, apple-silicon (got '$Profile')"
    }
}
if ($Repo -eq '') {
    Fail '-Repo must not be empty'
}

if ([string]::IsNullOrEmpty($env:LOCALAPPDATA)) {
    Fail 'LOCALAPPDATA is not set; this installer targets Windows'
}

# The uv installer and uv itself live under the user profile; LOCALAPPDATA is
# already known to be set, so it is a safe fallback for an empty USERPROFILE.
$ProfileHome = $env:USERPROFILE
if ([string]::IsNullOrEmpty($ProfileHome)) { $ProfileHome = $env:LOCALAPPDATA }

if ($Dir -eq '') {
    $Dir = Join-Path $env:LOCALAPPDATA 'agent-voice'
}
$InstallDir = (Resolve-FullPath $Dir).TrimEnd('\')

if ($InstallDir -eq $env:LOCALAPPDATA.TrimEnd('\')) {
    Fail '-Dir must not be LOCALAPPDATA itself'
}
if ($InstallDir.TrimEnd('\') -eq [System.IO.Path]::GetPathRoot($InstallDir).TrimEnd('\')) {
    Fail '-Dir must not be a drive root'
}

$DataDir = Join-Path (Split-Path -Parent $InstallDir) 'agent-voice-data'
$EnginesDir = Join-Path $InstallDir 'engines'
$EnvFile = Join-Path $InstallDir '.env'
$ShimDir = Join-Path $InstallDir 'bin'
$Shim = Join-Path $ShimDir 'agent-voice.cmd'

if ($Uninstall) {
    Invoke-Uninstall
    exit 0
}

$Git = Assert-Git

# Refuse a bad install dir before anything is installed or cloned.
Test-Checkout

# Resolve what to install. -Version wins; -Ref is explicit and unpinned;
# otherwise the latest release tag, never main.
if ($Version -ne '') {
    if ($Version -match '^\d+\.\d+\.\d+$') { $Version = 'v' + $Version }
    if ($Version -notmatch '^v\d+\.\d+\.\d+$') {
        Fail "-Version must be a release tag such as v0.6.0 (got '$Version')"
    }
    $Target = $Version
    $TargetSource = '-Version'
}
elseif ($Ref -ne '') {
    $Target = $Ref
    $Version = ''
    $TargetSource = '-Ref'
}
else {
    $Version = Get-LatestTag $Repo
    if ([string]::IsNullOrEmpty($Version)) {
        Fail "no vX.Y.Z release tag found in $Repo; pass -Version <tag> or -Ref <branch|sha>"
    }
    $Target = $Version
    $TargetSource = 'latest release tag'
}

if ($DryRun) {
    Write-Say 'agent-voice installer -- DRY RUN (nothing will be written)'
}
else {
    Write-Say 'agent-voice installer'
}
Write-Say "  repo        $Repo"
Write-Say "  target      $Target ($TargetSource)"
Write-Say "  install dir $InstallDir"
Write-Say "  data dir    $DataDir"
Write-Say "  engines dir $EnginesDir"
Write-Say "  shim        $Shim"
if ($Profile -ne '') { Write-Say "  profile     $Profile" } else { Write-Say '  profile     auto' }
if ($Download) { Write-Say '  weights     download now (-Download)' }
elseif ($NoDownload) { Write-Say '  weights     no download (-NoDownload)' }
else { Write-Say '  weights     setup decides' }
if ($NoSetup) { Write-Say '  setup       skipped (-NoSetup)' } else { Write-Say '  setup       agent-voice setup' }
if (-not $Yes) { Write-Say '  prompts     enabled (pass -Yes for unattended)' }
else { Write-Say '  prompts     disabled (-Yes)' }
if (-not [string]::IsNullOrEmpty($env:AGENT_VOICE_HOME) -and $env:AGENT_VOICE_HOME -ne $InstallDir) {
    Write-Warn "AGENT_VOICE_HOME is set to '$($env:AGENT_VOICE_HOME)'; this installer writes '$InstallDir' into .env and the shim"
}

$Uv = Resolve-Uv

Sync-Checkout -Git $Git

Write-Say 'Syncing the root project with uv'
Invoke-Action -File $Uv -Arguments @('sync', '--project', $InstallDir)

if (-not $DryRun) {
    $consoleScript = Join-Path $InstallDir '.venv\Scripts\agent-voice.exe'
    if (-not (Test-Path -LiteralPath $consoleScript)) {
        Fail "uv sync finished but $consoleScript is missing"
    }
}

Ensure-Directory $DataDir
Write-EnvFile
Write-Shim
Write-PathWarning

if ($NoSetup) {
    Write-Say "Skipping 'agent-voice setup' (-NoSetup)."
}
else {
    Write-Say "Running 'agent-voice setup'"
    $setupArgs = @('setup')
    if ($Yes) { $setupArgs += '--yes' }
    if ($Profile -ne '') { $setupArgs += @('--profile', $Profile) }
    if ($Download) { $setupArgs += '--download' }
    if ($NoDownload) { $setupArgs += '--no-download' }
    $env:AGENT_VOICE_HOME = $InstallDir
    Invoke-Action -File (Join-Path $InstallDir '.venv\Scripts\agent-voice.exe') -Arguments $setupArgs
}

Write-Say ''
if ($DryRun) {
    Write-Say 'Dry run complete: no changes were made.'
}
else {
    if ([string]::IsNullOrEmpty($Version)) { $displayVersion = $Target } else { $displayVersion = $Version }
    Write-Say "agent-voice $displayVersion installed."
}
Write-Say "  install dir $InstallDir"
Write-Say "  data dir    $DataDir"
Write-Say "  shim        $Shim"
Write-Say "Next: add $ShimDir to PATH, then run 'agent-voice doctor'."
Write-Say 'Windows note: EXPERIMENTAL / NOT VERIFIED ON REAL WINDOWS.'
exit 0
