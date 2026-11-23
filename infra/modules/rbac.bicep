param appPrincipalId string
param appName string
param storageContainerResourceId string
param searchResourceId string
param downloadFunctionResourceId string
param administratorObjectIds array = []

resource search 'Microsoft.Search/searchServices@2025-05-01' existing = {
  name: 'srch-shareiarag'
}

resource storage 'Microsoft.Storage/storageAccounts@2023-05-01' existing = {
  name: 'stshareiaragfn'
}

resource blobService 'Microsoft.Storage/storageAccounts/blobServices@2023-05-01' existing = {
  parent: storage
  name: 'default'
}

resource stateContainer 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-05-01' existing = {
  parent: blobService
  name: 'rag-console-e8e7'
}

resource downloadFunction 'Microsoft.Web/sites@2026-08-01' existing = {
  name: 'func-shareiarag-dl'
}

var readerRoleId = 'acdd72a7-3385-48ef-bd42-f606fba81ae7'
var monitoringReaderRoleId = '43d0d8ad-25c7-4714-9337-8ba259a9fe05'
var searchContributorRoleId = '7ca78c08-252a-4471-8644-bb5ff32d4ba0'
var storageBlobContributorRoleId = 'ba92f5b4-2d11-453d-a403-e96b0029c9fe'
var customRoleName = 'ShareIARAG Console Function Config Reader'
var customRoleGuid = '4c2b9d1c-6b16-4e8f-9ab5-1c35b3d8e8e7'
module inventoryPermissions 'inventory-rbac.bicep' = {
  name: 'rag-console-inventory-permissions'
  params: {
    appPrincipalId: appPrincipalId
    appName: appName
    searchResourceId: searchResourceId
    administratorObjectIds: administratorObjectIds
  }
}

resource customRole 'Microsoft.Authorization/roleDefinitions@2022-04-01' = {
  name: customRoleGuid
  properties: {
    roleName: customRoleName
    description: 'Read and update narrowly scoped deployment configuration for the download Function.'
    type: 'CustomRole'
    permissions: [
      {
        actions: [
          'Microsoft.Web/sites/read'
          'Microsoft.Web/sites/config/read'
          'Microsoft.Web/sites/config/list/action'
          'Microsoft.Web/sites/config/write'
        ]
        notActions: []
      }
    ]
    assignableScopes: [
      resourceGroup().id
    ]
  }
}

resource readerOnRg 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(resourceGroup().id, appName, readerRoleId, 'rg')
  properties: {
    principalId: appPrincipalId
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', readerRoleId)
    principalType: 'ServicePrincipal'
  }
}

resource monitoringReaderOnRg 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(resourceGroup().id, appName, monitoringReaderRoleId, 'rg')
  properties: {
    principalId: appPrincipalId
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', monitoringReaderRoleId)
    principalType: 'ServicePrincipal'
  }
}

resource searchContributor 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(searchResourceId, appName, searchContributorRoleId)
  scope: search
  properties: {
    principalId: appPrincipalId
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', searchContributorRoleId)
    principalType: 'ServicePrincipal'
  }
}

resource storageContributor 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(storageContainerResourceId, appName, storageBlobContributorRoleId)
  scope: stateContainer
  properties: {
    principalId: appPrincipalId
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', storageBlobContributorRoleId)
    principalType: 'ServicePrincipal'
  }
}

resource functionConfigRoleAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(downloadFunctionResourceId, appName, customRoleGuid)
  scope: downloadFunction
  properties: {
    principalId: appPrincipalId
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', customRoleGuid)
    principalType: 'ServicePrincipal'
  }
  dependsOn: [
    customRole
  ]
}
