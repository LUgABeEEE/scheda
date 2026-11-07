#Requires -Version 5.1
<#
.SYNOPSIS
    Register (or replace) the Windows Scheduled Task that keeps a scheda mirror
    and SQLite FTS5 index fresh on this machine.

.DESCRIPTION
    The task runs exactly one command, once a day:

        <python.exe> -m scheda refresh --quiet --home "<Home>"

    "refresh" is sync + index (src/scheda/cli.py cmd_refresh) and returns a
    non-zero exit code when the source is unreachable, so Task Scheduler's
    "Last Result" column is a usable health signal: 0 means the mirror and the
    index are current.

    ASCII ONLY ON PURPOSE: Windows PowerShell 5.1 decodes a .ps1 file without a
    BOM as ANSI, so a UTF-8 Chinese comment becomes mojibake and can break the
    parser. Keep every byte of this file in the 0x00-0x7F range.

.PARAMETER PythonExe
    Interpreter that can import scheda. Defaults to python.exe from PATH.
    Example: C:\Python312\python.exe
    Pass the interpreter, not scheda.exe: -m scheda behaves the same and always
    resolves to the same environment the check below validated.

.PARAMETER Home
    State directory (mirror, index, state/manifest.json, logs, config.json).
    Required. Example: C:\Users\<you>\.scheda
    Created if missing; it must be local and writable. Never point it at the
    shared network directory: the mirror and index are per-machine derivatives.

.PARAMETER TaskName
    Scheduled task name. Default: "scheda refresh"

.PARAMETER At
    Daily start time in 24-hour "HH:mm". Default: 09:00

.PARAMETER RunLevel
    Limited (default) or Highest. Limited is un-elevated, which is all a refresh
    needs; use Highest only if the state directory is ACL-restricted to admins.

.PARAMETER LogonType
    Interactive (default) runs the task only while this user is logged on.
    S4U runs it whether or not the user is logged on and stores no password, but
    drive letters mapped in the interactive session are NOT available under it --
    use a UNC path or -Source smb for the bundle in that case.

.PARAMETER Source
    Optional explicit source for the refresh: auto (default), local, mount, smb
    or webdav. Maps to the --source flag.

.PARAMETER Mount
    Path of the mounted share when -Source mount is used.
    Example: \\192.0.2.10\kb  or  Z:\kb

.PARAMETER Force
    Re-register even when a task with the same name is currently running.

.EXAMPLE
    .\register-refresh-task.ps1 -PythonExe C:\Python312\python.exe -Home C:\Users\<you>\.scheda

.EXAMPLE
    .\register-refresh-task.ps1 -Home D:\kb-state -Source mount -Mount '\\192.0.2.10\kb' -At 07:30

.NOTES
    Verify afterwards:
        Get-ScheduledTaskInfo -TaskName "scheda refresh"
        Start-ScheduledTask   -TaskName "scheda refresh"
        Get-ScheduledTaskInfo -TaskName "scheda refresh" |
            Format-List LastRunTime, LastTaskResult, NextRunTime
#>
[CmdletBinding()]
param(
    [string] $PythonExe = '',

    [Parameter(Mandatory = $true)]
    [Alias('Home')]
    [string] $KbHome = '',

    [string] $TaskName = 'scheda refresh',

    [string] $At = '09:00',

    [ValidateSet('Limited', 'Highest')]
    [string] $RunLevel = 'Limited',

    [ValidateSet('Interactive', 'S4U')]
    [string] $LogonType = 'Interactive',

    [ValidateSet('auto', 'local', 'mount', 'smb', 'webdav')]
    [string] $Source = '',

    [string] $Mount = '',

    [switch] $Force
)

$ErrorActionPreference = 'Stop'

# ---------------------------------------------------------------------------
# 1. interpreter
# ---------------------------------------------------------------------------
if ($PythonExe) {
    if (-not (Test-Path -LiteralPath $PythonExe)) {
        throw "PythonExe not found: $PythonExe"
    }
    $PythonExe = (Resolve-Path -LiteralPath $PythonExe).ProviderPath
}
else {
    $cmd = Get-Command python.exe -ErrorAction SilentlyContinue
    if (-not $cmd) {
        throw 'python.exe is not on PATH. Pass -PythonExe C:\path\to\python.exe'
    }
    $PythonExe = $cmd.Source
}

# Fail here, not silently every morning: the scheduled task runs under a clean
# environment, so "wrong interpreter" is the most common silent failure.
$probe = & $PythonExe -c "import scheda; print(scheda.__version__)" 2>&1
if ($LASTEXITCODE -ne 0) {
    throw "cannot import scheda with ${PythonExe}:`n$probe"
}
Write-Host ("scheda {0} importable from {1}" -f ($probe -join ' ').Trim(), $PythonExe)

# ---------------------------------------------------------------------------
# 2. state directory
# ---------------------------------------------------------------------------
$KbHome = $KbHome.Trim().Trim('"')
if ($KbHome -eq '~') {
    $KbHome = $env:USERPROFILE
}
elseif ($KbHome -like '~*') {
    $KbHome = Join-Path $env:USERPROFILE $KbHome.Substring(2)
}
$KbHome = [System.IO.Path]::GetFullPath($KbHome)
if (-not (Test-Path -LiteralPath $KbHome)) {
    New-Item -ItemType Directory -Path $KbHome -Force | Out-Null
    Write-Host "created state directory: $KbHome"
}

# ---------------------------------------------------------------------------
# 3. trigger
# ---------------------------------------------------------------------------
try {
    $when = [datetime]::ParseExact($At, 'HH:mm', [System.Globalization.CultureInfo]::InvariantCulture)
}
catch {
    throw "invalid -At '$At': expected 24-hour HH:mm, for example 09:00"
}
$trigger = New-ScheduledTaskTrigger -Daily -At $when

# ---------------------------------------------------------------------------
# 4. action
# ---------------------------------------------------------------------------
# --home is accepted both before and after the subcommand (cli.py
# _add_common_flags), so this spelling matches docs/deployment.md.
$argList = '-m scheda refresh --quiet --home "{0}"' -f $KbHome
if ($Source) {
    $argList += (' --source {0}' -f $Source)
}
if ($Mount) {
    $argList += (' --mount "{0}"' -f $Mount)
}

$action = New-ScheduledTaskAction -Execute $PythonExe -Argument $argList -WorkingDirectory $KbHome

# ---------------------------------------------------------------------------
# 5. settings and principal
# ---------------------------------------------------------------------------
# StartWhenAvailable: catch up after sleep/hibernate.
# ExecutionTimeLimit: a stale network mount must not hold the task forever.
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Minutes 15) `
    -MultipleInstances IgnoreNew

$userId = '{0}\{1}' -f $env:USERDOMAIN, $env:USERNAME
$principal = New-ScheduledTaskPrincipal -UserId $userId -LogonType $LogonType -RunLevel $RunLevel

# ---------------------------------------------------------------------------
# 6. register
# ---------------------------------------------------------------------------
$existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($existing) {
    if (-not $Force) {
        Write-Host "replacing existing task: $TaskName (pass -Force to acknowledge silently)"
    }
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
}

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
    -Settings $settings -Principal $principal `
    -Description 'scheda: refresh mirror and SQLite FTS5 index (scheda refresh --quiet)' | Out-Null

# ---------------------------------------------------------------------------
# 7. report + verification commands
# ---------------------------------------------------------------------------
Write-Host ''
Write-Host 'registered.'
Write-Host ("  task name : {0}" -f $TaskName)
Write-Host ("  python    : {0}" -f $PythonExe)
Write-Host ("  command   : {0} {1}" -f $PythonExe, $argList)
Write-Host ("  state dir : {0}" -f $KbHome)
Write-Host ("  daily at  : {0}  (RunLevel={1}, LogonType={2})" -f $when.ToString('HH:mm'), $RunLevel, $LogonType)
Write-Host ''
Write-Host 'verify with:'
Write-Host ("  Get-ScheduledTaskInfo -TaskName '{0}'" -f $TaskName)
Write-Host ("  Start-ScheduledTask   -TaskName '{0}'" -f $TaskName)
Write-Host ("  Get-ScheduledTaskInfo -TaskName '{0}' | Format-List LastRunTime, LastTaskResult, NextRunTime" -f $TaskName)
Write-Host '  # LastTaskResult 0x0 = success; 0x1 = the refresh failed (check the source mount)'
Write-Host ("  & '{0}' -m scheda --home '{1}' status --json" -f $PythonExe, $KbHome)
Write-Host ''
Write-Host 'uninstall:'
Write-Host ("  Unregister-ScheduledTask -TaskName '{0}' -Confirm:`$false" -f $TaskName)

exit 0
