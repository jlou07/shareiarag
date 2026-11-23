param suffix string
param deployerObjectId string
param administratorObjectIds array
param testUserObjectIds array
param searchName string
param foundryAccountName string
param foundryProjectName string
param filesAccountName string
param stateAccountName string
param consoleAppName string
param mcpAppName string
param downloadAppName string
param vaultName string

resource search 'Microsoft.Search/searchServices@2025-05-01' existing = {
  name: searchName
}
resource foundry 'Microsoft.CognitiveServices/accounts@2025-06-01' existing = {
  name: foundryAccountName
}
resource project 'Microsoft.CognitiveServices/accounts/projects@2025-06-01' existing = {
  parent: foundry
  name: foundryProjectName
}
resource files 'Microsoft.Storage/storageAccounts@2025-01-01' existing = {
  name: filesAccountName
}
resource state 'Microsoft.Storage/storageAccounts@2025-01-01' existing = {
  name: stateAccountName
}
resource blobService 'Microsoft.Storage/storageAccounts/blobServices@2025-01-01' existing = {
  parent: state
  name: 'default'
}
resource consoleContainer 'Microsoft.Storage/storageAccounts/blobServices/containers@2025-01-01' existing = {
  parent: blobService
  name: 'rag-console-test-${suffix}'
}
resource vault 'Microsoft.KeyVault/vaults@2024-11-01' existing = {
  name: vaultName
}
resource consoleApp 'Microsoft.Web/sites@2024-11-01' existing = {
  name: consoleAppName
}
resource mcpApp 'Microsoft.Web/sites@2024-11-01' existing = {
  name: mcpAppName
}
resource downloadApp 'Microsoft.Web/sites@2024-11-01' existing = {
  name: downloadAppName
}

var reader = 'acdd72a7-3385-48ef-bd42-f606fba81ae7'
var monitoringReader = '43d0d8ad-25c7-4714-9337-8ba259a9fe05'
var searchContributor = '7ca78c08-252a-4471-8644-bb5ff32d4ba0'
var blobContributor = 'ba92f5b4-2d11-453d-a403-e96b0029c9fe'
var blobOwner = 'b7e6dc6d-f1e8-4753-8033-0f276bb0955b'
var queueContributor = '974c5e8b-45b9-4653-ba55-5f855dd0fb88'
var filePrivilegedContributor = '69566ab7-960f-475b-8e7c-b3118f30c6bd'
var vaultOfficer = 'b86a8fe4-44ce-4948-aee5-eccb2c155cd7'
var openAiUser = '5e0bd9bd-7b93-4f28-af87-19fc36ad61bd'
var foundryProjectManager = 'eadc314b-1a2d-4efa-be10-5d325db5065e'
var foundryAgentConsumer = 'eed3b665-ab3a-47b6-8f48-c9382fb1dad6'

resource functionConfigRole 'Microsoft.Authorization/roleDefinitions@2022-04-01' = {
  name: guid(resourceGroup().id, 'shareiarag-test-function-config-${suffix}')
  properties: {
    roleName: 'ShareIARAG Test Function Config ${suffix}'
    description: 'Read and update configuration of the test download Function.'
    type: 'CustomRole'
    assignableScopes: [
      resourceGroup().id
    ]
    permissions: [
      {
        actions: [
          'Microsoft.Web/sites/read'
          'Microsoft.Web/sites/config/read'
          'Microsoft.Web/sites/config/list/action'
          'Microsoft.Web/sites/config/write'
        ]
      }
    ]
  }
}

resource indexPurgerRole 'Microsoft.Authorization/roleDefinitions@2022-04-01' = {
  name: guid(resourceGroup().id, 'shareiarag-test-index-purger-${suffix}')
  properties: {
    roleName: 'ShareIARAG Test Index Purger ${suffix}'
    description: 'Read the protected test index with elevated access and purge deleted-file chunks.'
    type: 'CustomRole'
    assignableScopes: [
      resourceGroup().id
    ]
    permissions: [
      {
        actions: []
        dataActions: [
          'Microsoft.Search/searchServices/indexes/documents/read'
          'Microsoft.Search/searchServices/indexes/contentSecurity/elevatedOperations/read'
          'Microsoft.Search/searchServices/indexes/documents/delete'
          'Microsoft.Search/searchServices/indexes/documents/write'
        ]
      }
    ]
  }
}

resource mcpReadRole 'Microsoft.Authorization/roleDefinitions@2022-04-01' = {
  name: guid(resourceGroup().id, 'shareiarag-test-mcp-search-${suffix}')
  properties: {
    roleName: 'ShareIARAG Test MCP Search Reader ${suffix}'
    description: 'Read test knowledge source metadata and retrieve test index content.'
    type: 'CustomRole'
    assignableScopes: [
      resourceGroup().id
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

resource mcpOperatorRole 'Microsoft.Authorization/roleDefinitions@2022-04-01' = {
  name: guid(resourceGroup().id, 'shareiarag-test-mcp-operator-${suffix}')
  properties: {
    roleName: 'ShareIARAG Test MCP Operator ${suffix}'
    description: 'Read the test MCP configuration and start or stop the test MCP app.'
    type: 'CustomRole'
    assignableScopes: [
      resourceGroup().id
    ]
    permissions: [
      {
        actions: [
          'Microsoft.Web/sites/read'
          'Microsoft.Web/sites/config/read'
          'Microsoft.Web/sites/config/list/action'
          'Microsoft.Web/sites/start/action'
          'Microsoft.Web/sites/stop/action'
        ]
      }
    ]
  }
}

resource agentReaderRole 'Microsoft.Authorization/roleDefinitions@2022-04-01' = {
  name: guid(resourceGroup().id, 'shareiarag-test-agent-reader-${suffix}')
  properties: {
    roleName: 'ShareIARAG Test Agent Reader ${suffix}'
    description: 'Read agents in the isolated Foundry project.'
    type: 'CustomRole'
    assignableScopes: [
      resourceGroup().id
    ]
    permissions: [
      {
        actions: []
        dataActions: [
          'Microsoft.CognitiveServices/accounts/AIServices/agents/read'
        ]
      }
    ]
  }
}

resource consoleReader 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(resourceGroup().id, consoleApp.id, reader)
  properties: {
    principalId: consoleApp.identity.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', reader)
  }
}
resource consoleMonitoringReader 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(resourceGroup().id, consoleApp.id, monitoringReader)
  properties: {
    principalId: consoleApp.identity.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', monitoringReader)
  }
}
resource consoleSearchContributor 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: search
  name: guid(search.id, consoleApp.id, searchContributor)
  properties: {
    principalId: consoleApp.identity.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', searchContributor)
  }
}
resource consoleIndexPurger 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: search
  name: guid(search.id, consoleApp.id, indexPurgerRole.id)
  properties: {
    principalId: consoleApp.identity.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: indexPurgerRole.id
  }
}
resource administratorIndexPurgers 'Microsoft.Authorization/roleAssignments@2022-04-01' = [
  for administratorId in administratorObjectIds: {
    scope: search
    name: guid(search.id, administratorId, indexPurgerRole.id)
    properties: {
      principalId: administratorId
      principalType: 'User'
      roleDefinitionId: indexPurgerRole.id
    }
  }
]
resource consoleStateContributor 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: consoleContainer
  name: guid(consoleContainer.id, consoleApp.id, blobContributor)
  properties: {
    principalId: consoleApp.identity.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', blobContributor)
  }
}
resource consoleFunctionConfig 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: downloadApp
  name: guid(downloadApp.id, consoleApp.id, functionConfigRole.id)
  properties: {
    principalId: consoleApp.identity.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: functionConfigRole.id
  }
}
resource consoleMcpOperator 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: mcpApp
  name: guid(mcpApp.id, consoleApp.id, mcpOperatorRole.id)
  properties: {
    principalId: consoleApp.identity.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: mcpOperatorRole.id
  }
}
resource consoleAgentReader 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: project
  name: guid(project.id, consoleApp.id, agentReaderRole.id)
  properties: {
    principalId: consoleApp.identity.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: agentReaderRole.id
  }
}
resource mcpSearchReader 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: search
  name: guid(search.id, mcpApp.id, mcpReadRole.id)
  properties: {
    principalId: mcpApp.identity.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: mcpReadRole.id
  }
}
resource projectSearchReader 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: search
  name: guid(search.id, project.id, mcpReadRole.id)
  properties: {
    principalId: project.identity.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: mcpReadRole.id
  }
}
resource searchOpenAiUser 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: foundry
  name: guid(foundry.id, search.id, openAiUser)
  properties: {
    principalId: search.identity.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', openAiUser)
  }
}
resource projectOpenAiUser 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: foundry
  name: guid(foundry.id, project.id, openAiUser)
  properties: {
    principalId: project.identity.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', openAiUser)
  }
}
resource deployerFoundryProjectManager 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: project
  name: guid(project.id, deployerObjectId, foundryProjectManager)
  properties: {
    principalId: deployerObjectId
    principalType: 'User'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', foundryProjectManager)
  }
}
resource testerFoundryAgentConsumers 'Microsoft.Authorization/roleAssignments@2022-04-01' = [
  for testerId in testUserObjectIds: {
    scope: project
    name: guid(project.id, testerId, foundryAgentConsumer)
    properties: {
      principalId: testerId
      principalType: 'User'
      roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', foundryAgentConsumer)
    }
  }
]
resource testerFoundryProjectReaders 'Microsoft.Authorization/roleAssignments@2022-04-01' = [
  for testerId in testUserObjectIds: {
    scope: project
    name: guid(project.id, testerId, reader)
    properties: {
      principalId: testerId
      principalType: 'User'
      roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', reader)
    }
  }
]
resource functionBlobOwner 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: state
  name: guid(state.id, downloadApp.id, blobOwner)
  properties: {
    principalId: downloadApp.identity.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', blobOwner)
  }
}
resource functionQueueContributor 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: state
  name: guid(state.id, downloadApp.id, queueContributor)
  properties: {
    principalId: downloadApp.identity.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', queueContributor)
  }
}
resource deployerVaultOfficer 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: vault
  name: guid(vault.id, deployerObjectId, vaultOfficer)
  properties: {
    principalId: deployerObjectId
    principalType: 'User'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', vaultOfficer)
  }
}
resource deployerSearchContributor 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: search
  name: guid(search.id, deployerObjectId, searchContributor)
  properties: {
    principalId: deployerObjectId
    principalType: 'User'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', searchContributor)
  }
}
resource deployerFileContributor 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: files
  name: guid(files.id, deployerObjectId, filePrivilegedContributor)
  properties: {
    principalId: deployerObjectId
    principalType: 'User'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', filePrivilegedContributor)
  }
}
