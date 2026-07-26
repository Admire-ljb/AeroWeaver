$ErrorActionPreference = "Stop"

Write-Host "Using Python:" (python --version)
python scripts/check_repository.py
python -m compileall -q .
python -m pytest

Push-Location ui
try {
  npm install
  npm run lint
  npm run build
}
finally {
  Pop-Location
}

Write-Host "mock demo smoke checks passed"
Write-Host "Start the backend with: `$env:SIM_ADAPTER='mock'; python server.py"
