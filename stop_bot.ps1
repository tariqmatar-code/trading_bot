# stop_bot.ps1 — stop the running trading bot process
$taskName = "TradingBotJT"

# Stop the scheduled task (marks it as stopped in Task Scheduler)
Stop-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue

# Kill any lingering pythonw.exe running JT.py
Get-WmiObject Win32_Process | Where-Object {
    $_.Name -like "python*" -and $_.CommandLine -like "*JT.py*"
} | ForEach-Object {
    Write-Host "Killing PID $($_.ProcessId): $($_.CommandLine)"
    Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
}

Write-Host "Bot stopped." -ForegroundColor Yellow
