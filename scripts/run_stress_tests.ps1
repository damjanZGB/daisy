# Runs the Lufthansa agent stress-test suite described in docs/agent_stress_testing.md
param(
    [string]$FunctionName = "daisy_in_action-0k2c0",
    [string]$Region = "us-west-2",
    [string]$PayloadPattern = "test_event*.json",
    [string]$PayloadFolder = "test_suite",
    [string]$AwsCliPath = "C:\Program Files\Amazon\AWSCLIV2\aws.exe",
    [switch]$FailFast
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

if (-not (Test-Path $PayloadFolder)) {
    throw "Payload folder '$PayloadFolder' not found."
}

$payloads = Get-ChildItem -Path $PayloadFolder -Filter $PayloadPattern | Sort-Object Name
if (-not $payloads) {
    throw "No payloads matching '$PayloadPattern' found in '$PayloadFolder'."
}

$timestamp = Get-Date -Format "yyyyMMdd-HHmmss"
$resultsDir = Join-Path $PayloadFolder ("results_" + $timestamp)
New-Item -ItemType Directory -Path $resultsDir | Out-Null

Write-Host "Running stress suite against function '$FunctionName' in region '$Region'"
Write-Host ("Found {0} payload(s)." -f $payloads.Count)
Write-Host ("Results will be stored in {0}`n" -f $resultsDir)

$summary = @()
foreach ($payload in $payloads) {
    $name = [System.IO.Path]::GetFileNameWithoutExtension($payload.Name)
    $outputFile = Join-Path $resultsDir ("out_{0}.json" -f $name)
    # Skip non-Lambda event descriptors (e.g., helper test configs)
    $rawPayload = Get-Content $payload.FullName -Raw
    if (($rawPayload -notmatch '"apiPath"') -and ($rawPayload -notmatch '"function"') -and ($rawPayload -notmatch '"messageVersion"')) {
        Write-Host ("Skipping {0} (not a Lambda event payload)" -f $payload.Name)
        continue
    }
    $invokeArgs = @(
        "lambda", "invoke",
        "--function-name", $FunctionName,
        "--region", $Region,
        "--payload", ("fileb://{0}" -f $payload.FullName),
        $outputFile,
        "--cli-binary-format", "raw-in-base64-out"
    )
    Write-Host ("Invoking {0}..." -f $payload.Name)
    & $AwsCliPath @invokeArgs | Out-Null

    $content = Get-Content $outputFile -Raw | ConvertFrom-Json
    $response = $content.response
    $statusCode = $response.httpStatusCode
    $body = $response.responseBody.'application/json'.body | ConvertFrom-Json
    $offersCount = 0
    if ($null -ne $body -and ($body.PSObject.Properties.Name -contains 'offers') -and $body.offers) {
        $offersCount = ($body.offers | Measure-Object).Count
    }
    $hasError = $statusCode -ne 200 -or $offersCount -eq 0

    $summary += [pscustomobject]@{
        Scenario    = $payload.Name
        StatusCode  = $statusCode
        Offers      = $offersCount
        OutputFile  = $outputFile
        Error       = if ($hasError -and ($body.PSObject.Properties.Name -contains 'error')) { $body.error } else { $null }
    }

    if ($hasError -and $FailFast) {
        Write-Warning ("Scenario {0} failed (status {1}). Halting suite." -f $payload.Name, $statusCode)
        break
    }
}

$summary | Format-Table -AutoSize | Out-String | Write-Host

$failures = $summary | Where-Object { $_.StatusCode -ne 200 -or $_.Offers -eq 0 }
if ($failures) {
    Write-Error "Stress suite completed with failures. Inspect output files above before deploying."
    exit 1
}

Write-Host "Stress suite completed successfully. All scenarios returned ≥1 offer." -ForegroundColor Green
