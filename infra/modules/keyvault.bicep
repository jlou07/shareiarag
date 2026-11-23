param location string
param vaultName string
param tenantId string
param filesAccountName string
param consolePrincipalId string
param downloadPrincipalId string

@description('Storage account key used in the connection string (0 = key1, 1 = key2). Switch before regenerating the other key.')
@allowed([0, 1])
param filesKeyIndex int = 1

var secretsUserRoleId = '4633458b-17de-408a-b874-0445c86b69e6'

resource files 'Microsoft.Storage/storageAccounts@2023-05-01' existing = {
  name: filesAccountName
}

resource vault 'Microsoft.KeyVault/vaults@2024-11-01' = {
  name: vaultName
  location: location
  properties: {
    tenantId: tenantId
    sku: {
      family: 'A'
      name: 'standard'
    }
    enableRbacAuthorization: true
    enableSoftDelete: true
    softDeleteRetentionInDays: 90
    enablePurgeProtection: true
    publicNetworkAccess: 'Enabled'
  }
}

// The account key is read inside Azure Resource Manager and never leaves it:
// Azure Files indexers only accept account-key connection strings.
resource filesConnection 'Microsoft.KeyVault/vaults/secrets@2024-11-01' = {
  parent: vault
  name: 'files-connection-${filesAccountName}'
  properties: {
    contentType: 'Azure Storage connection string'
    value: 'DefaultEndpointsProtocol=https;AccountName=${files.name};AccountKey=${files.listKeys().keys[filesKeyIndex].value};EndpointSuffix=${environment().suffixes.storage}'
  }
}

resource consoleSecretsUser 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(filesConnection.id, consolePrincipalId, secretsUserRoleId)
  scope: filesConnection
  properties: {
    principalId: consolePrincipalId
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', secretsUserRoleId)
    principalType: 'ServicePrincipal'
  }
}

resource downloadSecretsUser 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(filesConnection.id, downloadPrincipalId, secretsUserRoleId)
  scope: filesConnection
  properties: {
    principalId: downloadPrincipalId
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', secretsUserRoleId)
    principalType: 'ServicePrincipal'
  }
}

output vaultName string = vault.name
output secretName string = filesConnection.name
