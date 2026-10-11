<#
.SYNOPSIS
    Build and deploy the Google Agent Platform for Copilot Chat - Proxy Cloud Run service.
.DESCRIPTION
    Uses Cloud Build to create the image from the Dockerfile in the api folder and updates
    the existing service, leaving IAM and configuration to Terraform.
.PARAMETER ProjectId
    Google Cloud project hosting the service (the proxy project).
.PARAMETER Region
    Cloud Run region of the service.
.PARAMETER ServiceName
    Cloud Run service name.
#>
param(
    [Parameter(Mandatory = $true)][string]$ProjectId,
    [Parameter(Mandatory = $true)][string]$Region,
    [string]$ServiceName = "copilot-chat-proxy"
)

$ErrorActionPreference = "Stop"
$PSNativeCommandUseErrorActionPreference = $false

if (-not (Get-Command gcloud -ErrorAction SilentlyContinue)) {
    Write-Error "Google Cloud SDK (gcloud) not found in PATH. Install and configure gcloud first."
}

$apiDir = Split-Path -Parent $MyInvocation.MyCommand.Path
if (-not (Test-Path (Join-Path $apiDir "Dockerfile")) -or
    -not (Test-Path (Join-Path $apiDir "requirements.txt")) -or
    -not (Test-Path (Join-Path $apiDir "src\main.py"))) {
    Write-Error "Incomplete API context: expected Dockerfile, requirements.txt and src\main.py in $apiDir."
}

Write-Host "Building and deploying '$ServiceName' ($ProjectId / $Region)..." -ForegroundColor Cyan
gcloud run deploy $ServiceName `
    --source $apiDir `
    --region $Region `
    --project $ProjectId `
    --quiet
if ($LASTEXITCODE -ne 0) {
    Write-Error "Cloud Build or Cloud Run deployment failed (exit code $LASTEXITCODE)."
}

$serviceUrl = gcloud run services describe $ServiceName `
    --region $Region `
    --project $ProjectId `
    --format "value(status.url)"
if ($LASTEXITCODE -ne 0) {
    Write-Error "Deployment succeeded but the service URL could not be read."
}

Write-Host "Deployment complete: $serviceUrl" -ForegroundColor Green
