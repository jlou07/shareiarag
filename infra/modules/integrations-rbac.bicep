// Lets the console's MCP and Foundry tabs work under its managed identity.
param appPrincipalId string
param appName string
param mcpAppName string = 'app-shareiarag-mcp-dev-68e5'
param foundryAccountName string = 'aif-shareiarag'
param foundryProjectName string = 'proj-shareiarag'

resource mcpApp 'Microsoft.Web/sites@2024-11-01' existing = {
  name: mcpAppName
}

resource foundryAccount 'Microsoft.CognitiveServices/accounts@2025-06-01' existing = {
  name: foundryAccountName
}

resource foundryProject 'Microsoft.CognitiveServices/accounts/projects@2025-06-01' existing = {
  parent: foundryAccount
  name: foundryProjectName
}

var mcpRoleGuid = guid(resourceGroup().id, 'ShareIARAG Console MCP Operator')
var agentRoleGuid = guid(resourceGroup().id, 'ShareIARAG Console Agent Reader')

resource mcpRole 'Microsoft.Authorization/roleDefinitions@2022-04-01' = {
  name: mcpRoleGuid
  properties: {
    roleName: 'ShareIARAG Console MCP Operator'
    description: 'Read the MCP Web App configuration and start or stop it.'
    type: 'CustomRole'
    permissions: [
      {
        actions: [
          'Microsoft.Web/sites/read'
          'Microsoft.Web/sites/config/read'
          'Microsoft.Web/sites/config/list/action'
          'Microsoft.Web/sites/start/action'
          'Microsoft.Web/sites/stop/action'
        ]
        notActions: []
      }
    ]
    assignableScopes: [
      resourceGroup().id
    ]
  }
}

resource agentRole 'Microsoft.Authorization/roleDefinitions@2022-04-01' = {
  name: agentRoleGuid
  properties: {
    roleName: 'ShareIARAG Console Agent Reader'
    description: 'List Foundry agents (read only).'
    type: 'CustomRole'
    permissions: [
      {
        actions: []
        notActions: []
        dataActions: [
          'Microsoft.CognitiveServices/accounts/AIServices/agents/read'
        ]
        notDataActions: []
      }
    ]
    assignableScopes: [
      resourceGroup().id
    ]
  }
}

resource mcpAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(mcpApp.id, appName, mcpRoleGuid)
  scope: mcpApp
  properties: {
    principalId: appPrincipalId
    roleDefinitionId: mcpRole.id
    principalType: 'ServicePrincipal'
  }
}

resource agentAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(foundryProject.id, appName, agentRoleGuid)
  scope: foundryProject
  properties: {
    principalId: appPrincipalId
    roleDefinitionId: agentRole.id
    principalType: 'ServicePrincipal'
  }
}
