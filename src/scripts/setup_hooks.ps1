# Install git pre-push hook for AI log submission (Windows PowerShell).
# Run once after cloning: powershell -ExecutionPolicy Bypass -File src\scripts\setup_hooks.ps1

$ErrorActionPreference = 'Stop'

$HookFile = '.git/hooks/pre-push'

# Git on Windows runs hooks through its POSIX shell. Use `sh` explicitly so a
# WindowsApps `bash.exe` alias cannot intercept the logging commands.
$HookBody = @'
#!/usr/bin/env sh
# Pre-push: sweep recent Antigravity / Gemini prompts, then submit AI logs.
sh src/scripts/_pyrun.sh src/scripts/log_antigravity.py --auto || true
sh src/scripts/_pyrun.sh src/scripts/submit_log.py || true
exit 0
'@

Set-Content -Path $HookFile -Value $HookBody -Encoding UTF8 -NoNewline
Write-Host "[ai-log] Git pre-push hook installed."

if (-not (Test-Path .ai-log)) { New-Item -ItemType Directory -Path .ai-log | Out-Null }
if (-not (Test-Path .ai-log/.gitkeep)) { New-Item -ItemType File -Path .ai-log/.gitkeep | Out-Null }

Write-Host "[ai-log] Setup complete. Configure AI_LOG_SERVER in your .env file."
