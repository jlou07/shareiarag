$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$global:ConsoleDeployTest = @{
    clientId = '11111111-1111-4111-8111-111111111111'
    objectId = '22222222-2222-4222-8222-222222222222'
    commands = [System.Collections.Generic.List[string]]::new()
    refuseDelete = $false
    archiveVerified = $false
    reuse = $false
}
$global:ConsoleDeployTest.registration = @{
    appId = $global:ConsoleDeployTest.clientId
    id = $global:ConsoleDeployTest.objectId
    displayName = 'shareiarag-console-e8e7'
    signInAudience = 'AzureADMyOrg'
    tags = @('shareiarag-console-e8e7')
    api = @{ oauth2PermissionScopes = @() }
    spa = @{ redirectUris = @('https://app-shareiarag-console-e8e7.azurewebsites.net/auth/callback') }
    requiredResourceAccess = @(@{
        resourceAppId = 'existing-api'
        resourceAccess = @(@{ id = 'existing-permission'; type = 'Role' })
    })
}

$global:ConsoleDeployTest.localParameters = Join-Path ([System.IO.Path]::GetTempPath()) "shareiarag-console-$([guid]::NewGuid().ToString('N')).json"
@{ parameters = @{
    resourceGroupName = @{ value = 'rg-under-test' }
    adminObjectIds = @{ value = '88888888-8888-4888-8888-888888888888' }
    downloadAuthClientId = @{ value = '99999999-9999-4999-8999-999999999999' }
} } | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $global:ConsoleDeployTest.localParameters
$env:SHAREIARAG_CONFIRM_RG = 'rg-under-test'

function Assert-Test {
    param([bool]$Condition, [string]$Message)
    if (-not $Condition) { throw $Message }
}

# All cloud and HTTP commands are shadowed in this process only.
function az {
    param([Parameter(ValueFromRemainingArguments = $true)][string[]]$Arguments)
    $global:LASTEXITCODE = 0
    $command = $Arguments -join ' '
    $global:ConsoleDeployTest.commands.Add($command)
    $result = switch -Regex ($command) {
        '^account set ' { @{}; break }
        '^account show ' { @{ id = '55555555-5555-4555-8555-555555555555'; tenantId = '66666666-6666-4666-8666-666666666666' }; break }
        '^ad app list ' { @(); break }
        '^ad app show ' { $global:ConsoleDeployTest.registration; break }
        '^ad sp list .*servicePrincipalNames' {
            @(@{
                appId = 'search-api'; id = 'search-principal'
                oauth2PermissionScopes = @(@{ id = 'search-scope'; value = 'user_impersonation'; isEnabled = $true })
            }); break
        }
        '^ad sp list ' { @(); break }
        '^ad sp create ' { @{}; break }
        '^rest --method POST --uri https://graph.microsoft.com/v1.0/applications ' {
            $bodyPath = $Arguments[[array]::IndexOf($Arguments, '--body') + 1]
            Assert-Test ($bodyPath.StartsWith('@')) 'JSON must use a file to avoid Windows CLI quoting loss.'
            $body = Get-Content -LiteralPath $bodyPath.Substring(1) -Raw | ConvertFrom-Json
            Assert-Test ($body.spa.redirectUris.Count -eq 1) 'SPA redirect is missing.'
            $global:ConsoleDeployTest.registration; break
        }
        '^rest --method PATCH --uri https://graph.microsoft.com/v1.0/applications/' {
            $bodyPath = $Arguments[[array]::IndexOf($Arguments, '--body') + 1]
            $body = Get-Content -LiteralPath $bodyPath.Substring(1) -Raw | ConvertFrom-Json
            Assert-Test ($body.api.requestedAccessTokenVersion -eq 2) 'API does not request v2 tokens.'
            Assert-Test ($body.api.oauth2PermissionScopes[0].value -eq 'Console.Access') 'Scope is missing.'
            Assert-Test ([bool]$body.api.oauth2PermissionScopes[0].userConsentDescription) 'User consent is incomplete.'
            $search = @($body.requiredResourceAccess | Where-Object { $_.resourceAppId -eq 'search-api' })
            Assert-Test ($search.Count -eq 1) 'Delegated Search API permission is missing.'
            Assert-Test ($search[0].resourceAccess[0].id -eq 'search-scope' -and
                $search[0].resourceAccess[0].type -eq 'Scope') 'Search permission is not delegated.'
            Assert-Test (@($body.requiredResourceAccess | Where-Object { $_.resourceAppId -eq 'existing-api' }).Count -eq 1) 'Existing API permissions were lost.'
            if ($global:ConsoleDeployTest.reuse) {
                Assert-Test ($body.api.oauth2PermissionScopes[0].id -eq '33333333-3333-4333-8333-333333333333') 'Redeploy changed the scope ID and invalidated consent.'
            }
            @{}; break
        }
        '^webapp identity assign .*--name func-shareiarag-dl ' { @{ principalId = '77777777-7777-4777-8777-777777777777' }; break }
        '^deployment sub what-if ' {
            Assert-Test ($command.Contains("entraClientId=$($global:ConsoleDeployTest.clientId)")) 'What-if does not use the real client ID.'
            Assert-Test ($command.Contains('--no-pretty-print')) 'What-if must return machine-readable JSON.'
            Assert-Test ($command.Contains("@$($global:ConsoleDeployTest.localParameters)")) 'What-if does not use the local parameters file.'
            @{ changes = @(@{ changeType = $(if ($global:ConsoleDeployTest.refuseDelete) { 'Delete' } else { 'Create' }); resourceId = '/test' }) }; break
        }
        '^deployment sub create ' { @{}; break }
        '^rest --method GET --uri .*basicPublishingCredentialsPolicies/scm' {
            @{ properties = @{ allow = $false } }; break
        }
        '^rest --method GET --uri .*Microsoft.Web/sites/app-shareiarag-console-e8e7\?api-version=' {
            @{ properties = @{ defaultHostName = 'app-shareiarag-console-e8e7-generated.azurewebsites.net' } }; break
        }
        '^rest --method PUT --uri .*basicPublishingCredentialsPolicies/scm' { @{}; break }
        '^webapp deploy ' {
            Assert-Test ($command.Contains('--async true')) 'Long Oryx builds must not block a synchronous upload connection.'
            Assert-Test ($command.Contains('--track-status false')) 'CLI startup tracking must not fail on the transient in-place Oryx restart.'
            $path = $Arguments[[array]::IndexOf($Arguments, '--src-path') + 1]
            $zip = [System.IO.Compression.ZipFile]::OpenRead($path)
            try {
                $entries = @($zip.Entries | ForEach-Object FullName)
                Assert-Test ($entries.Count -eq 15) 'ZIP does not contain exactly the allowlisted files.'
                Assert-Test ($entries -contains 'documents.py') 'ZIP lost the file inventory controller.'
                Assert-Test ($entries -contains 'source_connections.py') 'ZIP lost the multi-account source controller.'
                Assert-Test ($entries -contains 'static/index.html') 'ZIP lost the static directory.'
                Assert-Test ($entries -contains 'static/app.bundle.js') 'ZIP lost the built bundle.'
                Assert-Test (-not ($entries | Where-Object { $_ -match 'venv|node_modules|\.env|test_' })) 'ZIP contains local/test files.'
                $global:ConsoleDeployTest.archiveVerified = $true
            } finally { $zip.Dispose() }
            @{ id = '44444444-4444-4444-8444-444444444444' }; break
        }
        '^webapp log deployment list ' {
            Assert-Test ($command.Contains("id=='44444444-4444-4444-8444-444444444444'")) 'Build polling must track the exact upload UUID.'
            @{ id = '44444444-4444-4444-8444-444444444444'; status = 4 }; break
        }
        default { throw "Unexpected CLI command in isolated deployment test: $command" }
    }
    if ($command -match '^ad app list ') {
        if ($global:ConsoleDeployTest.reuse) { return (ConvertTo-Json -InputObject @($global:ConsoleDeployTest.registration) -Depth 15 -Compress) }
        return '[]'
    }
    if ($command -match '^ad sp list ' -and $command -notmatch 'servicePrincipalNames') {
        if ($global:ConsoleDeployTest.reuse) { return '[{"id":"existing-principal"}]' }
        return '[]'
    }
    $result | ConvertTo-Json -Depth 15 -Compress
}

function Invoke-WebRequest {
    param([string]$Uri, [int]$TimeoutSec, [switch]$SkipHttpErrorCheck)
    Assert-Test ($Uri.StartsWith('https://app-shareiarag-console-e8e7-generated.azurewebsites.net/')) 'Probe assumed an incorrect hostname.'
    if ($Uri.EndsWith('/healthz')) { return @{ StatusCode = 200; Content = '{"status":"ok"}' } }
    if ($Uri.EndsWith('/api/me')) { return @{ StatusCode = 401; Content = '{}' } }
    throw "Unexpected HTTP probe: $Uri"
}

function Invoke-RestMethod {
    param([string]$Uri, [int]$TimeoutSec)
    Assert-Test ($Uri.EndsWith('/public-config')) 'Unexpected public configuration URI.'
    return @{ clientId = $global:ConsoleDeployTest.clientId }
}

& (Join-Path $PSScriptRoot 'deploy-console.ps1') -LocalParameters $global:ConsoleDeployTest.localParameters | Out-Null
Assert-Test $global:ConsoleDeployTest.archiveVerified 'Code deployment did not inspect the ZIP.'
$whatIfPosition = $global:ConsoleDeployTest.commands.FindIndex([Predicate[string]] { param($value) $value -match '^deployment sub what-if ' })
$createPosition = $global:ConsoleDeployTest.commands.FindIndex([Predicate[string]] { param($value) $value -match '^deployment sub create ' })
Assert-Test ($whatIfPosition -ge 0 -and $createPosition -gt $whatIfPosition) 'Create ran before mandatory what-if.'
$identityPosition = $global:ConsoleDeployTest.commands.FindIndex([Predicate[string]] { param($value) $value -match '^webapp identity assign ' })
Assert-Test ($identityPosition -ge 0 -and $identityPosition -lt $whatIfPosition) 'The download Function identity must exist before Key Vault role assignment.'
Assert-Test (-not (Test-Path -LiteralPath (Join-Path $PSScriptRoot 'console-deploy.zip'))) 'Temporary ZIP was not cleaned.'

$global:ConsoleDeployTest.commands.Clear()
$global:ConsoleDeployTest.refuseDelete = $true
$refused = $false
try { & (Join-Path $PSScriptRoot 'deploy-console.ps1') -LocalParameters $global:ConsoleDeployTest.localParameters | Out-Null }
catch {
    if ($_.Exception.Message -notmatch 'Safety preflight refused') { throw }
    $refused = $true
}
Assert-Test $refused 'Destructive what-if was not rejected.'
Assert-Test (-not ($global:ConsoleDeployTest.commands | Where-Object { $_ -match '^deployment sub create ' })) 'Provisioning ran after a destructive what-if.'
$global:ConsoleDeployTest.commands.Clear()
$global:ConsoleDeployTest.refuseDelete = $false
$global:ConsoleDeployTest.reuse = $true
$global:ConsoleDeployTest.registration.api.oauth2PermissionScopes = @(@{
    id = '33333333-3333-4333-8333-333333333333'; value = 'Console.Access'
})
& (Join-Path $PSScriptRoot 'deploy-console.ps1') -LocalParameters $global:ConsoleDeployTest.localParameters | Out-Null
Assert-Test (-not ($global:ConsoleDeployTest.commands | Where-Object { $_ -match '^ad sp create ' })) 'Redeploy tried to recreate the existing principal.'
Remove-Item -LiteralPath $global:ConsoleDeployTest.localParameters -Force
Remove-Item Env:SHAREIARAG_CONFIRM_RG
Remove-Variable -Name ConsoleDeployTest -Scope Global
Write-Output 'PASS: isolated CLI binding, Entra reuse/consent stability, ZIP contents, hostname, SCM protection and destructive what-if checks.'
