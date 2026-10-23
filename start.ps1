$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
& "$PSScriptRoot\.venv\Scripts\python.exe" -m uvicorn backend.app.main:app --host 127.0.0.1 --port 6190
