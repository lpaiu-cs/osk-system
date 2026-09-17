# Invoke explicitly after release/update and an isolated growth run have passed.
# Registration enables future model calls and vault writes; this file does not run itself.
[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$Python,
    [Parameter(Mandatory)][string]$CommandFile,
    [string]$Name = 'osk-domain-growth',
    [datetime]$At = '09:00'
)
$ErrorActionPreference = 'Stop'
$pythonPath = (Resolve-Path -LiteralPath $Python).Path
$commandPath = (Resolve-Path -LiteralPath $CommandFile).Path
$runnerPath = Join-Path $PSScriptRoot 'growth_run.py'
$vaultPath = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '../../..')).Path
if ($pythonPath.Contains('"') -or $commandPath.Contains('"') -or $runnerPath.Contains('"')) {
    throw 'Paths must not contain a quote.'
}
& $pythonPath $runnerPath --command-file $commandPath --check
if ($LASTEXITCODE -ne 0) { throw 'Growth command check failed; task was not registered.' }
if (Get-ScheduledTask -TaskName $Name -ErrorAction SilentlyContinue) {
    throw "Task '$Name' already exists. Review and update it explicitly."
}
$action = New-ScheduledTaskAction -Execute $pythonPath -WorkingDirectory $vaultPath `
    -Argument ('"' + $runnerPath + '" --command-file "' + $commandPath + '" --limit 3 --timeout 600')
$trigger = New-ScheduledTaskTrigger -Daily -At $At
$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 15)
$principal = New-ScheduledTaskPrincipal -UserId ([System.Security.Principal.WindowsIdentity]::GetCurrent().Name) `
    -LogonType Interactive -RunLevel Limited
Register-ScheduledTask -TaskName $Name -Action $action -Trigger $trigger `
    -Settings $settings -Principal $principal
