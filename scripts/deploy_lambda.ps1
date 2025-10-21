param(
  [string]$FunctionName = "daisy_in_action-0k2c0",
  [string]$Region = "us-west-2",
  [string]$Profile = "reStrike"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$root = Split-Path -Parent $MyInvocation.MyCommand.Path | Split-Path -Parent
$dist = Join-Path $root 'dist'
if (-not (Test-Path $dist)) { New-Item -ItemType Directory -Path $dist | Out-Null }

$staging = Join-Path $dist 'lambda_staging'
if (Test-Path $staging) { Remove-Item -Recurse -Force $staging }
New-Item -ItemType Directory -Path $staging | Out-Null

${awsDir} = Join-Path $root 'aws'
${srcFile} = Join-Path ${awsDir} 'lambda_function.py'
Copy-Item ${srcFile} -Destination $staging
${dataDir} = Join-Path $root 'data'
${catalogFile} = Join-Path ${dataDir} 'lh_destinations_catalog.json'
if (Test-Path ${catalogFile}) {
  New-Item -ItemType Directory -Path (Join-Path $staging 'data') | Out-Null
  Copy-Item ${catalogFile} -Destination (Join-Path $staging 'data')
}

$zipPath = Join-Path $dist 'lambda.zip'
if (Test-Path $zipPath) { Remove-Item $zipPath -Force }

Push-Location $staging
try {
  if (Get-Command compress-archive -ErrorAction SilentlyContinue) {
    Compress-Archive -Path * -DestinationPath $zipPath -Force
  } else {
    & powershell -NoProfile -Command "Compress-Archive -Path * -DestinationPath '$zipPath' -Force"
  }
} finally {
  Pop-Location
}

Write-Host "Updating Lambda: $FunctionName in $Region (profile $Profile) with package $zipPath"
aws lambda update-function-code --function-name $FunctionName --zip-file ("fileb://" + $zipPath) --region $Region --profile $Profile | Out-String | Write-Host
