targetScope = 'subscription'

@description('Microsoft Entra application client ID for the MCP API.')
@minLength(1)
param entraClientId string

@description('Comma-separated valid audiences for the MCP API tokens.')
@minLength(1)
param tokenAudiences string

@description('Comma-separated allowed client application IDs.')
@minLength(1)
param allowedClientIds string

@description('Required delegated scope name in incoming access tokens.')
@minLength(1)
param requiredScope string

@description('User-assigned identity trusted by the MCP Entra app through a federated identity credential (no client secret).')
param identityName string = 'id-shareiarag-mcp-68e5'

@description('Object ID of the deployment principal that manages the MCP secret.')
@minLength(1)
param deployerObjectId string

param location string = 'francecentral'
param resourceGroupName string = 'rg-shareiarag'
param planName string = 'plan-shareiarag-shared'
param appName string = 'app-shareiarag-mcp-dev-68e5'
param appInsightsName string = 'appi-shareiarag-mcp-dev-68e5'
param keyVaultName string = 'kv-shareiarag-e8e7'
param searchName string = 'srch-shareiarag'
@description('Log Analytics workspace resource ID (default: the subscription default workspace in France Central).')
param workspaceId string = '${subscription().id}/resourceGroups/DefaultResourceGroup-PAR/providers/Microsoft.OperationalInsights/workspaces/DefaultWorkspace-${subscription().subscriptionId}-PAR'
param createdAt string = utcNow()

@description('Optional traceability tags (for example the deployment session and author).')
param onboardSessionId string = ''
param deployedBy string = ''

var tags = union({
  'app-onboard-skill': 'true'
  'created-at': createdAt
  environment: 'shareiarag-mcp-dev-68e5'
}, empty(onboardSessionId) ? {} : { 'app-onboard-session-id': onboardSessionId },
  empty(deployedBy) ? {} : { 'deployed-by': deployedBy })

resource searchReadRole 'Microsoft.Authorization/roleDefinitions@2022-04-01' = {
  name: guid(subscription().id, 'shareiarag-mcp-read-68e5')
  properties: {
    roleName: 'ShareIARAG MCP Search Reader 68e5'
    description: 'Read knowledge source metadata and retrieve indexed content for the MCP service.'
    type: 'CustomRole'
    assignableScopes: [
      '/subscriptions/${subscription().subscriptionId}/resourceGroups/${resourceGroupName}'
    ]
    permissions: [
      {
        actions: [
          'Microsoft.Search/searchServices/knowledgeSources/read'
        ]
        dataActions: [
          'Microsoft.Search/searchServices/indexes/documents/read'
          'Microsoft.Search/searchServices/knowledgeBases/retrieve/action'
        ]
      }
    ]
  }
}

module mcp 'resources.bicep' = {
  name: 'shareiarag-mcp-resources-68e5'
  scope: resourceGroup(resourceGroupName)
  params: {
    location: location
    tags: tags
    planName: planName
    appName: appName
    appInsightsName: appInsightsName
    keyVaultName: keyVaultName
    searchName: searchName
    workspaceId: workspaceId
    tenantId: subscription().tenantId
    entraClientId: entraClientId
    tokenAudiences: tokenAudiences
    allowedClientIds: allowedClientIds
    requiredScope: requiredScope
    identityName: identityName
    deployerObjectId: deployerObjectId
    searchReadRoleId: searchReadRole.id
  }
}

output appId string = mcp.outputs.appId
output hostname string = mcp.outputs.hostname
