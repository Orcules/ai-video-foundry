# Run the API server with correct PYTHONPATH so tvd_pipeline (monolith) is found.
# Usage: .\run_server.ps1   or   powershell -File run_server.ps1
#
# For Google Sheets / GCS: put service_account.json in Comp_Videos\ or api_pipeline\,
# or set env SERVICE_ACCOUNT_FILE to its full path.

$root = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$env:PYTHONPATH = "$root;$root\Comp_Videos"
Set-Location $PSScriptRoot
python -m uvicorn server:app --host 0.0.0.0 --port 8000 --reload
