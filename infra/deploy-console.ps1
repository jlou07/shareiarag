[CmdletBinding()]
param(
    [string]$SubscriptionId,
    [string]$TenantId,
    [string]$Location = 'francecentral',
    [string]$LocalParameters = (Join-Path $PSScriptRoot 'main.parameters.local.json'),
    [string]$EntraClientId,
    [switch]$IncludeLocalRedirect,
    [switch]$SkipCodeDeploy
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
if ($PSVersionTable.PSVersion.Major -lt 7) { throw 'Run this helper with PowerShell 7 or later.' }
if (-not (Test-Path -LiteralPath $LocalParameters)) {
    throw "Local parameters file not found: $LocalParameters. Copy main.parameters.local.example.json and fill it in."
}
$localValues = (Get-Content -LiteralPath $LocalParameters -Raw | ConvertFrom-Json).parameters
foreach ($name in 'resourceGroupName', 'adminObjectIds', 'downloadAuthClientId') {
    if (-not $localValues.PSObject.Properties[$name] -or [string]::IsNullOrWhiteSpace([string]$localValues.$name.value) -or
        ([string]$localValues.$name.value).Contains('<')) {
        throw "Local parameters file must define '$name'."
    }
}
$ResourceGroup = [string]$localValues.resourceGroupName.value

$appName = 'app-shareiarag-console-e8e7'
$entraDisplayName = 'shareiarag-console-e8e7'
$redirectUri = "https://$appName.azurewebsites.net/auth/callback"
$localRedirectUri = 'http://localhost:8000/auth/callback'
$scopeValue = 'Console.Access'
$repoRoot = Split-Path -Parent $PSScriptRoot
$template = Join-Path $PSScriptRoot 'main.bicep'
$parameters = Join-Path $PSScriptRoot 'main.parameters.json'
$zipPath = Join-Path $PSScriptRoot 'console-deploy.zip'

function Invoke-AzJson {
    param([Parameter(ValueFromRemainingArguments = $true)][string[]]$Arguments)
    Invoke-Az -Arguments $Arguments -ReadJson
}

function Invoke-Az {
    param(
        [Parameter(ValueFromRemainingArguments = $true)][string[]]$Arguments,
        [switch]$ReadJson
    )
    $bodyFile = $null
    try {
        $bodyIndex = [array]::IndexOf($Arguments, '--body')
        if ($bodyIndex -ge 0 -and $Arguments[$bodyIndex + 1].StartsWith('{')) {
            $bodyFile = [System.IO.Path]::GetTempFileName()
            Set-Content -LiteralPath $bodyFile -Value $Arguments[$bodyIndex + 1] -Encoding utf8NoBOM
            $Arguments[$bodyIndex + 1] = "@$bodyFile"
        }
        $result = & az @Arguments --only-show-errors
        if ($LASTEXITCODE -ne 0) {
            throw "Azure CLI failed ($LASTEXITCODE): az $($Arguments[0..1] -join ' ')"
        }
        if ($ReadJson) { return (($result -join [Environment]::NewLine) | ConvertFrom-Json) }
        $result
    } finally {
        if ($bodyFile) { Remove-Item -LiteralPath $bodyFile -Force }
    }
}

if ($SubscriptionId) { Invoke-Az @('account', 'set', '--subscription', $SubscriptionId) }
$account = Invoke-AzJson @('account', 'show', '--output', 'json')
if ($SubscriptionId -and $account.id -ne $SubscriptionId) {
    throw "Active Azure CLI subscription '$($account.id)' does not match '$SubscriptionId'."
}
$SubscriptionId = [string]$account.id
if ($TenantId -and $account.tenantId -ne $TenantId) {
    throw "Active Azure CLI tenant '$($account.tenantId)' does not match '$TenantId'."
}
$TenantId = [string]$account.tenantId
$subscriptionScope = "/subscriptions/$SubscriptionId"
$existingPlanId = "$subscriptionScope/resourceGroups/$ResourceGroup/providers/Microsoft.Web/serverfarms/plan-shareiarag-shared"
$existingAppId = "$subscriptionScope/resourceGroups/$ResourceGroup/providers/Microsoft.Web/sites/app-shareiarag-mcp-dev-68e5"
Write-Host "Target: subscription $SubscriptionId, tenant $TenantId, resource group $ResourceGroup."
if (-not $env:SHAREIARAG_CONFIRM_RG) {
    $answer = Read-Host "Type the resource group name to confirm deployment"
    if ($answer -ne $ResourceGroup) { throw 'Confirmation did not match the resource group; nothing was changed.' }
} elseif ($env:SHAREIARAG_CONFIRM_RG -ne $ResourceGroup) {
    throw 'SHAREIARAG_CONFIRM_RG does not match the resource group; nothing was changed.'
}

function Get-AndValidateEntraApp {
    param([string]$ClientId)
    $app = Invoke-AzJson @('ad', 'app', 'show', '--id', $ClientId, '--output', 'json')
    if ($app.appId -ne $ClientId -or $app.displayName -ne $entraDisplayName) {
        throw "The supplied EntraClientId is not the expected console application; refusing reuse."
    }
    if ($app.signInAudience -ne 'AzureADMyOrg') {
        throw 'The existing Entra application is not single-tenant.'
    }
    if ($app.tags -notcontains 'shareiarag-console-e8e7') {
        throw 'This registration is not marked as the intended console; refusing to overwrite an unrelated app.'
    }
    $app
}

if ($EntraClientId) {
    $entraApp = Get-AndValidateEntraApp $EntraClientId
} else {
    $existing = @(Invoke-AzJson @('ad', 'app', 'list', '--display-name', $entraDisplayName, '--output', 'json'))
    if ($existing.Count -gt 1) {
        throw "Multiple Entra applications have display name '$entraDisplayName'; refusing to choose."
    }
    if ($existing.Count -eq 1) {
        $entraApp = Get-AndValidateEntraApp $existing[0].appId
        $EntraClientId = $entraApp.appId
    } else {
        $spaUris = @($redirectUri)
        if ($IncludeLocalRedirect) { $spaUris += $localRedirectUri }
        $createBody = @{
            displayName = $entraDisplayName
            signInAudience = 'AzureADMyOrg'
            tags = @('shareiarag-console-e8e7')
            spa = @{ redirectUris = $spaUris }
        } | ConvertTo-Json -Depth 6 -Compress
        $entraApp = Invoke-AzJson @('rest', '--method', 'POST',
            '--uri', 'https://graph.microsoft.com/v1.0/applications',
            '--headers', 'Content-Type=application/json', '--body', $createBody, '--output', 'json')
        $EntraClientId = $entraApp.appId
        $entraApp = Get-AndValidateEntraApp $EntraClientId
    }
}

$appObjectId = $entraApp.id
$existingScopes = @($entraApp.api.oauth2PermissionScopes | Where-Object { $_.value -eq $scopeValue })
if ($existingScopes.Count -gt 1) { throw 'Multiple Console.Access scopes were found.' }
$scopeId = if ($existingScopes.Count -eq 1) { $existingScopes[0].id } else { [guid]::NewGuid().Guid }
$redirectUris = @(@($entraApp.spa.redirectUris) + $redirectUri | Where-Object { $_ -ne $localRedirectUri } | Select-Object -Unique)
if ($IncludeLocalRedirect) { $redirectUris += $localRedirectUri }

$otherScopes = @($entraApp.api.oauth2PermissionScopes | Where-Object { $_.value -ne $scopeValue })
$searchPrincipals = @(Invoke-AzJson @('ad', 'sp', 'list',
    '--filter', "servicePrincipalNames/any(name:name eq 'https://search.azure.com')", '--output', 'json'))
if ($searchPrincipals.Count -ne 1) { throw 'Could not uniquely resolve the Azure AI Search service principal.' }
$searchPrincipal = $searchPrincipals[0]
$searchScopes = @($searchPrincipal.oauth2PermissionScopes | Where-Object { $_.value -eq 'user_impersonation' -and $_.isEnabled })
if ($searchScopes.Count -ne 1) { throw 'Azure AI Search does not expose the expected delegated scope.' }
$requiredAccess = @($entraApp.requiredResourceAccess | Where-Object { $_.resourceAppId -ne $searchPrincipal.appId })
$searchAccess = @($entraApp.requiredResourceAccess | Where-Object { $_.resourceAppId -eq $searchPrincipal.appId } |
    ForEach-Object { $_.resourceAccess } | Where-Object { $_.id -ne $searchScopes[0].id })
$requiredAccess += @{
    resourceAppId = $searchPrincipal.appId
    resourceAccess = @($searchAccess + @{ id = $searchScopes[0].id; type = 'Scope' })
}
$patch = @{
    signInAudience = 'AzureADMyOrg'
    identifierUris = @("api://$EntraClientId")
    api = @{
        requestedAccessTokenVersion = 2
        oauth2PermissionScopes = @($otherScopes + @{
            id = $scopeId
            adminConsentDescription = 'Allow the console API to be used.'
            adminConsentDisplayName = 'Use the console API'
            userConsentDescription = 'Access the RAG console API with your identity. Administrator authorization is checked separately.'
            userConsentDisplayName = 'Access RAG Control'
            isEnabled = $true
            type = 'User'
            value = $scopeValue
        })
    }
    spa = @{ redirectUris = $redirectUris }
    requiredResourceAccess = $requiredAccess
}
$patchJson = $patch | ConvertTo-Json -Depth 10 -Compress
Invoke-Az @('rest', '--method', 'PATCH',
    '--uri', "https://graph.microsoft.com/v1.0/applications/$appObjectId",
    '--headers', 'Content-Type=application/json', '--body', $patchJson)
$principals = @(Invoke-AzJson @('ad', 'sp', 'list', '--filter', "appId eq '$EntraClientId'", '--output', 'json'))
if ($principals.Count -gt 1) { throw 'Multiple service principals were found for the console.' }
if ($principals.Count -eq 0) {
    Invoke-Az @('ad', 'sp', 'create', '--id', $EntraClientId, '--only-show-errors')
}

Write-Host "Entra application: $EntraClientId"
Write-Host "Delegated scope: api://$EntraClientId/$scopeValue (scope id $scopeId)"
Write-Host 'Inventory scope: https://search.azure.com/user_impersonation (delegated; consent required).'
Write-Host 'Consent is not granted by this helper; obtain admin/user consent separately.'
Write-Host 'This helper does not grant Graph application permissions, change group members, or configure client secrets.'

$downloadIdentity = Invoke-AzJson @('webapp', 'identity', 'assign', '--resource-group', $ResourceGroup,
    '--name', 'func-shareiarag-dl', '--subscription', $SubscriptionId, '--output', 'json')
if ([string]::IsNullOrWhiteSpace([string]$downloadIdentity.principalId)) {
    throw 'The download Function system identity could not be enabled.'
}

$deploymentName = "shareiarag-console-$([guid]::NewGuid().ToString('N').Substring(0, 8))"
# main.bicep is subscription-scoped, so deployment sub what-if is the
# equivalent read-only preflight; deployment group what-if rejects this template.
$whatIf = Invoke-AzJson @('deployment', 'sub', 'what-if', '--name', $deploymentName,
    '--location', $Location, '--template-file', $template, '--parameters', "@$parameters", '--parameters', "@$LocalParameters",
    "entraClientId=$EntraClientId", '--subscription', $SubscriptionId,
    '--result-format', 'ResourceIdOnly', '--no-pretty-print', '--output', 'json')
$unsafe = @($whatIf.changes | Where-Object {
    $_.changeType -eq 'Delete' -or
    (($_.changeType -eq 'Modify') -and
      ($_.resourceId -like "$existingPlanId*" -or $_.resourceId -like "$existingAppId*"))
})
if ($unsafe.Count -gt 0) {
    $details = $unsafe | ForEach-Object { "[$($_.changeType)] $($_.resourceId)" }
    throw "Safety preflight refused provisioning because the what-if proposed: $($details -join '; ')"
}
Write-Host "Safety what-if passed: $deploymentName (no deletes or existing plan/app modifications)."
$portalUrl = "https://portal.azure.com/#view/Microsoft_Azure_Resources/DeploymentDetails.MenuView/~/overview/id/$([uri]::EscapeDataString("$subscriptionScope/providers/Microsoft.Resources/deployments/$deploymentName"))"
Write-Host $portalUrl

$bicepArgs = @('deployment', 'sub', 'create', '--name', $deploymentName,
    '--location', $Location, '--template-file', $template, '--parameters', "@$parameters", '--parameters', "@$LocalParameters",
    "entraClientId=$EntraClientId", '--subscription', $SubscriptionId, '--only-show-errors')
Invoke-Az $bicepArgs

$siteId = "$subscriptionScope/resourceGroups/$ResourceGroup/providers/Microsoft.Web/sites/$appName"
$site = Invoke-AzJson @('rest', '--method', 'GET',
    '--uri', "$($siteId)?api-version=2024-11-01", '--output', 'json')
$actualHost = [string]$site.properties.defaultHostName
if ([string]::IsNullOrWhiteSpace($actualHost)) {
    throw "The provisioned site did not return properties.defaultHostName."
}
$actualRedirectUri = "https://$actualHost/auth/callback"
if ($redirectUris -notcontains $actualRedirectUri) {
    $redirectUris = @($redirectUris + $actualRedirectUri | Select-Object -Unique)
    $patch.spa.redirectUris = $redirectUris
    $patchJson = $patch | ConvertTo-Json -Depth 10 -Compress
    Invoke-Az @('rest', '--method', 'PATCH',
        '--uri', "https://graph.microsoft.com/v1.0/applications/$appObjectId",
        '--headers', 'Content-Type=application/json', '--body', $patchJson)
}

if (-not $SkipCodeDeploy) {
    $consoleRoot = Join-Path $repoRoot 'console'
    $files = @('app.py', 'web.py', 'settings.py', 'auth.py', 'azure_gateway.py',
        'state.py', 'operations.py', 'index_rebuild.py', 'source_connections.py', 'documents.py', 'sync_files.py', 'integrations.py', 'requirements.txt', 'static\index.html', 'static\app.bundle.js')
    foreach ($file in $files) {
        if (-not (Test-Path -LiteralPath (Join-Path $consoleRoot $file))) {
            throw "Required package file is missing: $file. Build the browser bundle before deploying."
        }
    }
    try {
        if (Test-Path -LiteralPath $zipPath) { Remove-Item -LiteralPath $zipPath -Force }
        $archive = [System.IO.Compression.ZipFile]::Open($zipPath, [System.IO.Compression.ZipArchiveMode]::Create)
        try {
            foreach ($file in $files) {
                [System.IO.Compression.ZipFileExtensions]::CreateEntryFromFile(
                    $archive, (Join-Path $consoleRoot $file), $file.Replace('\', '/'),
                    [System.IO.Compression.CompressionLevel]::Optimal) | Out-Null
            }
        } finally { $archive.Dispose() }
        # Oryx rebuilds wwwroot in place, so a container recycled mid-build fails once before the final
        # restart succeeds; CLI startup tracking reports that transient failure. Health is verified below.
        $upload = Invoke-AzJson @('webapp', 'deploy', '--resource-group', $ResourceGroup, '--name', $appName,
            '--src-path', $zipPath, '--type', 'zip', '--timeout', '1800000',
            '--async', 'true', '--track-status', 'false', '--subscription', $SubscriptionId, '--output', 'json')
        $codeDeploymentId = [string]$upload.id
        $parsedId = [guid]::Empty
        if (-not [guid]::TryParse($codeDeploymentId, [ref]$parsedId)) {
            throw 'The async upload did not return a deployment UUID; inspect deployment history before retrying.'
        }
        $buildDeadline = [datetime]::UtcNow.AddMinutes(30)
        do {
            $build = Invoke-AzJson @('webapp', 'log', 'deployment', 'list',
                '--resource-group', $ResourceGroup, '--name', $appName,
                '--subscription', $SubscriptionId, '--query', "[?id=='$codeDeploymentId'] | [0]", '--output', 'json')
            if ($build -and $build.status -eq 3) { throw "Code build failed: $codeDeploymentId. Inspect its deployment logs." }
            if ($build -and $build.status -eq 4) { break }
            if ([datetime]::UtcNow -ge $buildDeadline) { throw "Code build timed out: $codeDeploymentId. No deployment success was assumed." }
            Write-Host "Waiting for code build $codeDeploymentId."
            Start-Sleep -Seconds 10
        } while ($true)
    } finally {
        if (Test-Path -LiteralPath $zipPath) { Remove-Item -LiteralPath $zipPath -Force }
        Invoke-Az @('rest', '--method', 'PUT',
            '--uri', "$siteId/basicPublishingCredentialsPolicies/scm?api-version=2023-12-01",
            '--body', '{"properties":{"allow":false}}', '--headers', 'Content-Type=application/json')
        $policy = Invoke-AzJson @('rest', '--method', 'GET',
            '--uri', "$siteId/basicPublishingCredentialsPolicies/scm?api-version=2023-12-01", '--output', 'json')
        if ($policy.properties.allow -ne $false) { throw 'SCM basic publishing authentication is not disabled.' }
    }
    $baseUrl = "https://$actualHost"
    $healthDeadline = [datetime]::UtcNow.AddMinutes(5)
    do {
        $health = $null
        try {
            $health = Invoke-WebRequest -Uri "$baseUrl/healthz" -TimeoutSec 30 -SkipHttpErrorCheck
        } catch [System.Net.Http.HttpRequestException], [System.Threading.Tasks.TaskCanceledException] {
            if ([datetime]::UtcNow -ge $healthDeadline) { throw }
        }
        if ($health -and $health.StatusCode -eq 200) { break }
        if ([datetime]::UtcNow -ge $healthDeadline) { break }
        Write-Warning 'The container is not responding yet; retrying the startup health check.'
        Start-Sleep -Seconds 15
    } while ($true)
    if (-not $health -or $health.StatusCode -ne 200 -or ($health.Content | ConvertFrom-Json).status -ne 'ok') { throw 'Hosted health verification failed.' }
    $publicConfig = Invoke-RestMethod -Uri "$baseUrl/public-config" -TimeoutSec 30
    if ($publicConfig.clientId -ne $EntraClientId) { throw 'Hosted client ID does not match the registration.' }
    $protected = Invoke-WebRequest -Uri "$baseUrl/api/me" -TimeoutSec 30 -SkipHttpErrorCheck
    if ($protected.StatusCode -ne 401) { throw 'Protected API did not return HTTP 401 without authentication.' }
    Write-Host "Verified hosted shell/API protection: $baseUrl"
}
