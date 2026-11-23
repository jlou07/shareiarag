targetScope = 'subscription'

@description('Azure region for the new console application.')
param location string = 'francecentral'

@description('Microsoft Entra application (client) ID approved for the console.')
param entraClientId string

@description('Enable group lookup after granting Group.ReadBasic.All to the console managed identity.')
param groupDirectoryEnabled bool = false

@description('Existing resource group that hosts the ShareIARAG resources.')
param resourceGroupName string = 'rg-shareiarag'

@description('Comma-separated Entra object IDs of the console administrators.')
param adminObjectIds string

@description('Client ID of the Entra application that protects the download Function.')
param downloadAuthClientId string

var subscriptionId = subscription().subscriptionId
var tenantId = tenant().tenantId
// Shared ShareIARAG plan (B2 Linux), also hosting the MCP Web App (infra/mcp).
var sharedPlanName = 'plan-shareiarag-shared'
var existingPlanId = '/subscriptions/${subscriptionId}/resourceGroups/${resourceGroupName}/providers/Microsoft.Web/serverfarms/${sharedPlanName}'
var existingStorageAccountName = 'stshareiaragfn'
var existingSearchName = 'srch-shareiarag'
var existingDownloadFunctionName = 'func-shareiarag-dl'
var consoleAppName = 'app-shareiarag-console-e8e7'
var consoleContainerName = 'rag-console-e8e7'
var keyVaultName = 'kv-shareiarag-e8e7'
var filesAccountName = 'shareiarag'

// The download Function is managed outside this template; deploy-console.ps1
// enables its system identity before this deployment so it can read Key Vault.
resource downloadFunction 'Microsoft.Web/sites@2024-11-01' existing = {
  name: existingDownloadFunctionName
  scope: resourceGroup(resourceGroupName)
}

module consoleApp 'modules/app-service.bicep' = {
  name: 'rag-console-app'
  scope: resourceGroup(resourceGroupName)
  params: {
    location: location
    appName: consoleAppName
    hostingPlanId: existingPlanId
    subscriptionId: subscriptionId
    resourceGroupName: resourceGroupName
    tenantId: tenantId
    entraClientId: entraClientId
    adminObjectIds: adminObjectIds
    searchServiceName: existingSearchName
    searchEndpoint: 'https://${existingSearchName}.search.windows.net'
    stateAccountName: existingStorageAccountName
    stateContainerName: consoleContainerName
    downloadAppName: existingDownloadFunctionName
    keyVaultName: keyVaultName
    filesAccountName: filesAccountName
    groupDirectoryEnabled: groupDirectoryEnabled
  }
}

module secrets 'modules/keyvault.bicep' = {
  name: 'rag-console-key-vault'
  scope: resourceGroup(resourceGroupName)
  params: {
    location: location
    vaultName: keyVaultName
    tenantId: tenantId
    filesAccountName: filesAccountName
    consolePrincipalId: consoleApp.outputs.principalId
    downloadPrincipalId: downloadFunction.identity.principalId
  }
}

module stateContainer 'modules/storage-container.bicep' = {
  name: 'rag-console-state-container'
  scope: resourceGroup(resourceGroupName)
  params: {
    storageAccountName: existingStorageAccountName
    containerName: consoleContainerName
  }
}

module downloadAuth 'modules/download-auth.bicep' = {
  name: 'rag-download-auth'
  scope: resourceGroup(resourceGroupName)
  params: {
    functionAppName: existingDownloadFunctionName
    tenantId: tenantId
    clientId: downloadAuthClientId
  }
}

module permissions 'modules/rbac.bicep' = {
  name: 'rag-console-permissions'
  scope: resourceGroup(resourceGroupName)
  params: {
    appPrincipalId: consoleApp.outputs.principalId
    appName: consoleAppName
    storageContainerResourceId: stateContainer.outputs.containerResourceId
    searchResourceId: resourceId(subscriptionId, resourceGroupName, 'Microsoft.Search/searchServices', existingSearchName)
    downloadFunctionResourceId: resourceId(subscriptionId, resourceGroupName, 'Microsoft.Web/sites', existingDownloadFunctionName)
    administratorObjectIds: split(adminObjectIds, ',')
  }
}

module integrationPermissions 'modules/integrations-rbac.bicep' = {
  name: 'rag-console-integration-permissions'
  scope: resourceGroup(resourceGroupName)
  params: {
    appPrincipalId: consoleApp.outputs.principalId
    appName: consoleAppName
  }
}

module planPermission 'modules/plan-rbac.bicep' = {
  name: 'rag-console-plan-permission'
  scope: resourceGroup(resourceGroupName)
  params: {
    appPrincipalId: consoleApp.outputs.principalId
    appName: consoleAppName
    planName: sharedPlanName
  }
}
