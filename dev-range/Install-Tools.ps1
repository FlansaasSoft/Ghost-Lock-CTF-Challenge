param([string]$Python = 'python')
$ErrorActionPreference = 'Stop'
# Project-local tools only. No WSL installation or system PATH changes.
$rangeRoot = $PSScriptRoot
$rangeTools = Join-Path $rangeRoot '.tools'
New-Item -ItemType Directory -Path $rangeTools -Force | Out-Null
$version = '1.17.2'
$archiveName = "terraform_${version}_windows_amd64.zip"
$archivePath = Join-Path $rangeTools $archiveName
$checksums = (Invoke-WebRequest "https://releases.hashicorp.com/terraform/$version/terraform_${version}_SHA256SUMS").Content
$matchingLines = @($checksums -split "`n" | Where-Object { $_.Trim().EndsWith(" $archiveName") })
if ($matchingLines.Count -ne 1) { throw 'Expected exactly one official Terraform checksum' }
$expected = ($matchingLines[0].Trim() -split '\s+')[0]
Invoke-WebRequest "https://releases.hashicorp.com/terraform/$version/$archiveName" -OutFile $archivePath
if ((Get-FileHash -LiteralPath $archivePath -Algorithm SHA256).Hash -ne $expected) { throw 'Terraform checksum mismatch' }
Expand-Archive -LiteralPath $archivePath -DestinationPath $rangeTools -Force
& $Python -m venv (Join-Path $rangeTools 'checks')
if ($LASTEXITCODE -ne 0) { throw 'Python 3.11+ is required' }
& (Join-Path $rangeTools 'checks\Scripts\python.exe') -m pip install --disable-pip-version-check -r (Join-Path $rangeRoot 'requirements-dev.txt')
if ($LASTEXITCODE -ne 0) { throw 'Verification dependency installation failed' }
Write-Output 'Project-local Terraform and verification tools installed. Ansible runs on the new Linux router.'

