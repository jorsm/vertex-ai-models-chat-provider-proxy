<#
.SYNOPSIS
    Download the OpenAPI 3.0.3 specification from the (IAM-protected) proxy and publish it
    on OpenAPI Viewer (https://www.openapiviewer.com).
.PARAMETER ProxyUrl
    Base URL of the deployed proxy, e.g. https://ai-companion-proxy-xxxx.a.run.app
#>
param(
    [Parameter(Mandatory = $true)][string]$ProxyUrl
)

$ErrorActionPreference = "Stop"

$specUrl = "$($ProxyUrl.TrimEnd('/'))/openapi.json"
$outputFile = Join-Path $PSScriptRoot "openapi.json"

Write-Host "Downloading $specUrl to $outputFile" -ForegroundColor Cyan

$token = (gcloud auth print-identity-token).Trim()
if (-not $token) {
    Write-Error "Could not obtain an OIDC token from gcloud. Run 'gcloud auth login' first."
}

& curl.exe -s -H "Authorization: Bearer $token" $specUrl -o $outputFile
if (-not (Test-Path $outputFile)) {
    Write-Error "The file $outputFile was not downloaded."
}

$rawText = [System.IO.File]::ReadAllText($outputFile, [System.Text.Encoding]::UTF8)
$jsonObj = $rawText | ConvertFrom-Json
if (-not $jsonObj.openapi) {
    Write-Error "The downloaded content is not a valid OpenAPI specification: $rawText"
}
Write-Host "OpenAPI $($jsonObj.openapi) downloaded: $($jsonObj.info.title) v$($jsonObj.info.version)" -ForegroundColor Green

Write-Host "Uploading to OpenAPI Viewer..." -ForegroundColor Cyan
$response = & curl.exe --progress-bar -X POST -F "file=@$outputFile;type=application/json;charset=utf-8" https://www.openapiviewer.com/api/upload
Write-Host "Response: $response"

try {
    $respJson = $response | ConvertFrom-Json
    $viewerUrl = if ($respJson.fullPath) { $respJson.fullPath } elseif ($respJson.id) { "https://www.openapiviewer.com/view/$($respJson.id)" } else { $null }
    if ($viewerUrl) {
        Write-Host "Interactive documentation: $viewerUrl" -ForegroundColor Yellow
    }
}
catch {
    Write-Verbose "Response was not JSON."
}
