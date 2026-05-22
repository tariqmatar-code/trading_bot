# ============================================================
# create_backup.ps1
# Creates a full backup ZIP to move the bot to any new PC.
# Run this from the trading_bot folder.
# ============================================================

$botDir   = "C:\Users\tariq_r8atash\trading_bot"
$desktop  = [Environment]::GetFolderPath("Desktop")
$stamp    = Get-Date -Format "yyyy-MM-dd_HH-mm"
$zipPath  = "$desktop\trading_bot_backup_$stamp.zip"

# Files to include in the backup
$include = @(
    "JT.py",
    ".env",
    "requirements.txt",
    "setup_autostart.ps1",
    "stop_bot.ps1",
    "setup_new_pc.ps1",
    "bot.py",
    "bot2.py",
    "claude_analyst.py",
    "config.py",
    "ig_client.py",
    "premarket_scanner.py",
    "reports.py",
    "scanner.py",
    "utils.py",
    ".gitignore"
)

# Also include epic_cache.json if it exists (saves re-lookup on new PC)
if (Test-Path "$botDir\epic_cache.json") {
    $include += "epic_cache.json"
}

# Build list of full paths that actually exist
$files = $include | ForEach-Object { "$botDir\$_" } | Where-Object { Test-Path $_ }

# Create ZIP
if (Test-Path $zipPath) { Remove-Item $zipPath -Force }

Add-Type -AssemblyName System.IO.Compression.FileSystem
$zip = [System.IO.Compression.ZipFile]::Open($zipPath, 'Create')
foreach ($file in $files) {
    $entryName = Split-Path $file -Leaf
    [System.IO.Compression.ZipFileExtensions]::CreateEntryFromFile(
        $zip, $file, $entryName, [System.IO.Compression.CompressionLevel]::Optimal
    ) | Out-Null
    Write-Host "  + $entryName"
}
$zip.Dispose()

$sizeMB = [math]::Round((Get-Item $zipPath).Length / 1MB, 2)
Write-Host ""
Write-Host "Backup created: $zipPath ($sizeMB MB)" -ForegroundColor Green
Write-Host ""
Write-Host "Transfer this ZIP to your cloud PC, then run setup_new_pc.ps1 inside it."
