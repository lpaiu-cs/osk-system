# Invoke explicitly after release/update and an isolated growth run have passed.
# Registration enables future model calls and vault writes; this file does not run itself.
# The task starts python itself, so its time limit (the engine's 1.5-hour budget + 30 minutes)
# stops the runner; a PowerShell wrapper that launches python with Start-Process is not stopped.
[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$Python,
    [Parameter(Mandatory)][string]$CommandFile,
    [string]$ClaudeCommandFile,
    [string]$Name = 'osk-domain-growth',
    [datetime]$At = '09:00'
)
$ErrorActionPreference = 'Stop'
$pythonPath = (Resolve-Path -LiteralPath $Python).Path
$commandPaths = @((Resolve-Path -LiteralPath $CommandFile).Path)
if ($ClaudeCommandFile) { $commandPaths += (Resolve-Path -LiteralPath $ClaudeCommandFile).Path }
$runnerPath = Join-Path $PSScriptRoot 'growth_run.py'
$vaultPath = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '../../..')).Path
if ($pythonPath.Contains('"') -or $runnerPath.Contains('"') -or ($commandPaths | Where-Object { $_.Contains('"') })) {
    throw 'Paths must not contain a quote.'
}
$files = ($commandPaths | ForEach-Object { '--command-file "' + $_ + '"' }) -join ' '
& $pythonPath $runnerPath @($commandPaths | ForEach-Object { '--command-file', $_ }) --check
if ($LASTEXITCODE -ne 0) { throw 'Growth command check failed; task was not registered.' }
if (Get-ScheduledTask -TaskName $Name -ErrorAction SilentlyContinue) {
    throw "Task '$Name' already exists. Review and update it explicitly."
}
$action = New-ScheduledTaskAction -Execute $pythonPath -WorkingDirectory $vaultPath `
    -Argument ('"' + $runnerPath + '" ' + $files + ' --daily')
$trigger = New-ScheduledTaskTrigger -Daily -At $At
$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 120) -Priority 5
$principal = New-ScheduledTaskPrincipal -UserId ([System.Security.Principal.WindowsIdentity]::GetCurrent().Name) `
    -LogonType Interactive -RunLevel Limited
Register-ScheduledTask -TaskName $Name -Action $action -Trigger $trigger `
    -Settings $settings -Principal $principal
