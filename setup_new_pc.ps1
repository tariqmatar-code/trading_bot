# ============================================================
# setup_new_pc.ps1
# Run this on the NEW cloud PC after extracting the backup ZIP.
# It installs Python (if missing), packages, and autostart.
# ============================================================

Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass -Force

$botDir = Split-Path -Parent $MyInvocation.MyCommand.Path

Write-Host "=== Trading Bot Setup ===" -ForegroundColor Cyan
Write-Host "Bot directory: $botDir"
Write-Host ""

# ── 1. Check Python ────────────────────────────────────────
Write-Host "Checking Python..." -ForegroundColor Yellow
$python = $null
foreach ($candidate in @("python", "python3", "py")) {
    try {
        $ver = & $candidate --version 2>&1
        if ($ver -match "Python 3\.") {
            $python = (Get-Command $candidate).Source
            Write-Host "  Found: $ver at $python" -ForegroundColor Green
            break
        }
    } catch {}
}

if (-not $python) {
    Write-Host ""
    Write-Host "Python 3 not found. Install it from:" -ForegroundColor Red
    Write-Host "  https://www.python.org/downloads/" -ForegroundColor Cyan
    Write-Host "  Tick 'Add Python to PATH' during install, then re-run this script."
    exit 1
}

# Derive pythonw.exe path (same folder as python.exe)
$pythonDir = Split-Path $python -Parent
$pythonw   = Join-Path $pythonDir "pythonw.exe"
if (-not (Test-Path $pythonw)) {
    # Some installs put pythonw.exe in the same dir
    $pythonw = $python  # fallback: use python.exe (will show a console window)
    Write-Host "  Note: pythonw.exe not found, will use python.exe" -ForegroundColor Yellow
}

# ── 2. Install packages ────────────────────────────────────
Write-Host ""
Write-Host "Installing Python packages..." -ForegroundColor Yellow
$req = Join-Path $botDir "requirements.txt"
if (Test-Path $req) {
    & $python -m pip install --upgrade pip --quiet
    & $python -m pip install -r $req
    Write-Host "  Packages installed." -ForegroundColor Green
} else {
    Write-Host "  requirements.txt not found — skipping." -ForegroundColor Red
}

# ── 3. Verify .env exists ──────────────────────────────────
Write-Host ""
$envFile = Join-Path $botDir ".env"
if (Test-Path $envFile) {
    Write-Host ".env found — credentials loaded from backup." -ForegroundColor Green
} else {
    Write-Host ".env NOT found. Create it manually with your credentials:" -ForegroundColor Red
    Write-Host @"

  IG_API_KEY=your_key
  IG_IDENTIFIER=your_username
  IG_PASSWORD=your_password
  IG_ACCOUNT_ID=
  IG_ACCOUNT_TYPE=DEMO
  TELEGRAM_BOT_TOKEN=your_token
  TELEGRAM_CHAT_ID=your_chat_id
  CLAUDE_API_KEY=your_claude_key

"@
}

# ── 4. Register Task Scheduler autostart ──────────────────
Write-Host ""
Write-Host "Registering autostart task..." -ForegroundColor Yellow

$taskName = "TradingBotJT"
$script   = Join-Path $botDir "JT.py"

Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue | Out-Null

$action   = New-ScheduledTaskAction `
    -Execute          $pythonw `
    -Argument         "`"$script`"" `
    -WorkingDirectory $botDir

$trigger  = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME

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

Write-Host "  Task '$taskName' registered — starts at every logon." -ForegroundColor Green

# ── 5. Start the bot now ───────────────────────────────────
Write-Host ""
Write-Host "Starting bot..." -ForegroundColor Yellow
schtasks /run /tn $taskName | Out-Null
Start-Sleep -Seconds 3

$proc = Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like "*JT.py*" }
if ($proc) {
    Write-Host "  Bot is running (PID $($proc.ProcessId))." -ForegroundColor Green
} else {
    Write-Host "  Bot started (silent background process)." -ForegroundColor Green
}

Write-Host ""
Write-Host "=== Setup Complete ===" -ForegroundColor Cyan
Write-Host "Bot will auto-start on every login."
Write-Host "To stop: run stop_bot.ps1"
Write-Host "Logs:    $botDir\jt_bot.log"
