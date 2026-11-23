param vaultName string
param filesAccountName string
param consoleAppName string
param downloadAppName string

resource vault 'Microsoft.KeyVault/vaults@2024-11-01' existing = {
  name: vaultName
}
resource filesSecret 'Microsoft.KeyVault/vaults/secrets@2024-11-01' existing = {
  parent: vault
  name: 'files-connection-${filesAccountName}'
}
resource downloadSecret 'Microsoft.KeyVault/vaults/secrets@2024-11-01' existing = {
  parent: vault
  name: 'download-provider-secret'
}
resource consoleApp 'Microsoft.Web/sites@2024-11-01' existing = {
  name: consoleAppName
}
resource downloadApp 'Microsoft.Web/sites@2024-11-01' existing = {
  name: downloadAppName
}

var secretsUser = '4633458b-17de-408a-b874-0445c86b69e6'

resource consoleFilesSecret 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: filesSecret
  name: guid(filesSecret.id, consoleApp.id, secretsUser)
  properties: {
    principalId: consoleApp.identity.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', secretsUser)
  }
}
resource downloadFilesSecret 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: filesSecret
  name: guid(filesSecret.id, downloadApp.id, secretsUser)
  properties: {
    principalId: downloadApp.identity.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', secretsUser)
  }
}
resource downloadProviderSecret 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: downloadSecret
  name: guid(downloadSecret.id, downloadApp.id, secretsUser)
  properties: {
    principalId: downloadApp.identity.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', secretsUser)
  }
}
