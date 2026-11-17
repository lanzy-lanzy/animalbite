[CmdletBinding(SupportsShouldProcess)]
param(
    [ValidatePattern('^([01][0-9]|2[0-3]):[0-5][0-9]$')]
    [string]$Time = '09:00',
    [string]$ProjectRoot = '',
    [string]$TaskName = 'ABTC_SMS_Reminders'
)

$ErrorActionPreference = 'Stop'
$identity = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
if (!$WhatIfPreference -and !$identity.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw 'Open PowerShell with Run as Administrator, then run this script to configure the logged-out daily SMS task. No task has been changed.'
}
if (!$ProjectRoot) {
    $ProjectRoot = Split-Path $PSScriptRoot -Parent
}
$ProjectRoot = (Resolve-Path $ProjectRoot).Path
$python = Join-Path $ProjectRoot '.venv\Scripts\python.exe'
$manage = Join-Path $ProjectRoot 'manage.py'
if (!(Test-Path $python) -or !(Test-Path $manage)) {
    throw 'Project virtual environment or manage.py is missing.'
}

$action = New-ScheduledTaskAction -Execute $python -Argument "`"$manage`" send_vaccination_reminders --type all" -WorkingDirectory $ProjectRoot
$manilaNow = [TimeZoneInfo]::ConvertTimeBySystemTimeZoneId((Get-Date), 'Singapore Standard Time')
$start = [datetime]::ParseExact($manilaNow.ToString('yyyy-MM-dd') + ' ' + $Time, 'yyyy-MM-dd HH:mm', [Globalization.CultureInfo]::InvariantCulture)
if ($start -le $manilaNow) {
    $start = $start.AddDays(1)
}
$trigger = New-ScheduledTaskTrigger -Daily -At $Time
$trigger.StartBoundary = $start.ToString('yyyy-MM-ddTHH:mm:ss') + '+08:00'
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -WakeToRun -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Hours 1)
$user = [Security.Principal.WindowsIdentity]::GetCurrent().Name
$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType S4U -RunLevel Limited

if ($PSCmdlet.ShouldProcess($TaskName, "Register daily SMS task at $Time Philippine time, including when logged out (no immediate SMS run)")) {
    $existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if ($existing) {
        $backup = Join-Path $env:TEMP ($TaskName + '-' + (Get-Date -Format 'yyyyMMddHHmmss') + '.xml')
        Export-ScheduledTask -TaskName $TaskName | Set-Content -LiteralPath $backup -Encoding UTF8
        Write-Output "Previous task configuration saved to $backup"
    }
    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Description 'ABTC advance, appointment-day and next-day missed reminders with seven-day recovery and backlog review. Uses project database and Semaphore configuration.' -Force | Out-Null
    Get-ScheduledTask -TaskName $TaskName | Select-Object TaskName, State, Principal
    Get-ScheduledTaskInfo -TaskName $TaskName | Select-Object NextRunTime, LastRunTime, LastTaskResult
}
