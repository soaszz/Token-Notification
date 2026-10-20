# Interactive local setup for Telegram secrets. Values go ONLY into Windows
# user environment variables — never into the repo, never into chat.
# Usage: powershell -ExecutionPolicy Bypass -File scripts\setup_telegram.ps1

$ErrorActionPreference = "Stop"

Write-Host ""
Write-Host "codex-reset-notifier - Telegram setup (local, secure)"
Write-Host "======================================================"
Write-Host ""
Write-Host "Steps (do this once):"
Write-Host "  1. In Telegram, open @BotFather -> /newbot -> follow prompts."
Write-Host "     Copy the bot token it gives you."
Write-Host "  2. Message your new bot once (send any text)."
Write-Host "  3. Open https://api.telegram.org/bot<TOKEN>/getUpdates in a browser"
Write-Host "     (replace <TOKEN>) and find your chat id in chat.id."
Write-Host "  4. Paste below. Values stay in this window + user env vars only."
Write-Host ""

$token = Read-Host -Prompt "Bot token (input is visible locally; do NOT paste into any chat)"
$chatId = Read-Host -Prompt "Chat id"

if (-not $token -or -not $chatId) { throw "Both values are required." }

[Environment]::SetEnvironmentVariable("TELEGRAM_BOT_TOKEN", $token, "User")
[Environment]::SetEnvironmentVariable("TELEGRAM_CHAT_ID", $chatId, "User")

# Refresh current session so notify.py can test immediately
$env:TELEGRAM_BOT_TOKEN = $token
$env:TELEGRAM_CHAT_ID = $chatId

Write-Host ""
Write-Host "Saved to user environment variables."
$reply = Read-Host "Send test message now? (y/n)"
if ($reply -eq "y") {
    Set-Location (Split-Path -Parent $PSScriptRoot)
    python notify.py --test
    if ($LASTEXITCODE -ne 0) { Write-Host "Test failed - check token/chat id." } else { Write-Host "Test OK - check Telegram." }
}
