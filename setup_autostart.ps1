# ============================================================
# setup_autostart.ps1
# Run this ONCE to register the trading bot as a Windows
# scheduled task that starts at logon (no console window).
# ============================================================

$pythonw  = "C:\Users\tariq_r8atash\AppData\Local\Python\pythoncore-3.14-64\pythonw.exe"
$script   = "C:\Users\tariq_r8atash\trading_bot\JT.py"
$workDir  = "C:\Users\tariq_r8atash\trading_bot"
$taskName = "TradingBotJT"

# Remove existing task if it exists
Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue

$action  = New-ScheduledTaskAction `
    -Execute  $pythonw `
    -Argument "`"$script`"" `
    -WorkingDirectory $workDir

# Trigger: run when THIS user logs in
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME

# Settings: auto-restart up to 3 times if it crashes (1-min delay each)
$settings = New-ScheduledTaskSettingsSet `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -MultipleInstances IgnoreNew

$principal = New-ScheduledTaskPrincipal `
    -UserId    $env:USERNAME `
    -LogonType Interactive `
    -RunLevel  Limited

Register-ScheduledTask `
    -TaskName  $taskName `
    -Action    $action `
    -Trigger   $trigger `
    -Settings  $settings `
    -Principal $principal `
    -Force | Out-Null

Write-Host "Task '$taskName' registered successfully." -ForegroundColor Green
Write-Host "The bot will start automatically next time you log in."
Write-Host ""
Write-Host "To start it right now without rebooting, run:"
Write-Host "  Start-ScheduledTask -TaskName '$taskName'"
Write-Host ""
Write-Host "To stop it, run:  stop_bot.ps1"
Write-Host "To remove it, run: Unregister-ScheduledTask -TaskName '$taskName' -Confirm:`$false"
