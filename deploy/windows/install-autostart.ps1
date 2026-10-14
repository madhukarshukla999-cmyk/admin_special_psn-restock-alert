# Starts the PSN stock bot automatically when you log in to Windows (no window, runs in the background).
# Run from the psn-stock-bot folder:
#   powershell -ExecutionPolicy Bypass -File deploy\windows\install-autostart.ps1
$ErrorActionPreference = "Stop"
$TaskName = "PSN Stock Bot"
$Root     = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$PyW      = Join-Path $Root ".venv\Scripts\pythonw.exe"
$Config   = Join-Path $Root "config.yaml"
$Log      = Join-Path $Root "data\bot.log"

if (-not (Test-Path $PyW))    { throw "Not found: $PyW`nRun 'python -m venv .venv' and install requirements first." }
if (-not (Test-Path (Join-Path $Root ".env"))) { throw "Missing .env in $Root (Telegram token + chat id)." }

$action   = New-ScheduledTaskAction -Execute $PyW `
              -Argument "-m psnbot -c `"$Config`" --log-file `"$Log`"" -WorkingDirectory $Root
$trigger  = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$settings = New-ScheduledTaskSettingsSet `
              -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
              -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
              -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings `
  -Principal $principal -Description "PlayStation gift card stock tracker (Telegram alerts)" -Force | Out-Null
Start-ScheduledTask -TaskName $TaskName

Start-Sleep -Seconds 5
$state = (Get-ScheduledTask -TaskName $TaskName).State
Write-Host ""
Write-Host "Installed '$TaskName' - it starts automatically when you log in. Current state: $state"
Write-Host "Logs:    $Log"
Write-Host "Stop:    schtasks /End /TN `"$TaskName`""
Write-Host "Start:   schtasks /Run /TN `"$TaskName`""
Write-Host "Remove:  powershell -ExecutionPolicy Bypass -File deploy\windows\uninstall-autostart.ps1"
