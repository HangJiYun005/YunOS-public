# PowerShell example
# Run this file from anywhere. Replace the export path with your local zip.

$ProjectRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
Set-Location $ProjectRoot

python .\tools\chatgpt_export_importer.py `
  ".\imports\chatgpt\export.zip" `
  --out ".\data\chatgpt_export"
