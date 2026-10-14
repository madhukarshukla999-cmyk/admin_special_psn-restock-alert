# Stops the bot and removes the auto-start task.
#   powershell -ExecutionPolicy Bypass -File deploy\windows\uninstall-autostart.ps1
$TaskName = "PSN Stock Bot"
if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "Removed '$TaskName'. The bot is stopped and won't start at login any more."
} else {
    Write-Host "'$TaskName' is not installed."
}
