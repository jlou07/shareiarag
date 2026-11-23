param appPrincipalId string
param appName string
param searchResourceId string
param administratorObjectIds array = []

resource search 'Microsoft.Search/searchServices@2025-05-01' existing = {
  name: last(split(searchResourceId, '/'))
}

var inventoryRoleGuid = 'dfce44f2-19d1-4a6e-bd01-62a61f75ce12'

resource inventoryRole 'Microsoft.Authorization/roleDefinitions@2022-04-01' = {
  name: inventoryRoleGuid
  properties: {
    roleName: 'ShareIARAG Console Index Inventory Reader'
    description: 'Read indexed file metadata for administration, without index content write permissions.'
    type: 'CustomRole'
    permissions: [
      {
        actions: []
        notActions: []
        dataActions: [
          'Microsoft.Search/searchServices/indexes/documents/read'
        ]
        notDataActions: []
      }
    ]
    assignableScopes: [
      resourceGroup().id
    ]
  }
}

resource inventoryReader 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(searchResourceId, appName, inventoryRoleGuid)
  scope: search
  properties: {
    principalId: appPrincipalId
    roleDefinitionId: inventoryRole.id
    principalType: 'ServicePrincipal'
  }
}

resource delegatedInventoryRole 'Microsoft.Authorization/roleDefinitions@2022-04-01' = {
  name: guid(inventoryRoleGuid, 'delegated')
  properties: {
    roleName: 'ShareIARAG Console Delegated Inventory Reader'
    description: 'Read protected index metadata through administrator-delegated elevated queries, without document writes.'
    type: 'CustomRole'
    permissions: [
      {
        actions: []
        notActions: []
        dataActions: [
          'Microsoft.Search/searchServices/indexes/documents/read'
          'Microsoft.Search/searchServices/indexes/contentSecurity/elevatedOperations/read'
        ]
        notDataActions: []
      }
    ]
    assignableScopes: [
      resourceGroup().id
    ]
  }
}

resource delegatedInventoryReaders 'Microsoft.Authorization/roleAssignments@2022-04-01' = [
  for administratorId in administratorObjectIds: {
    name: guid(searchResourceId, administratorId, 'delegated-inventory')
    scope: search
    properties: {
      principalId: administratorId
      roleDefinitionId: delegatedInventoryRole.id
      principalType: 'User'
    }
  }
]

// Targeted purge: read chunk keys and file URLs past the permission filter, then delete the
// chunks of missing files. No document write; the console falls back to a full rebuild without it.
resource purgeRole 'Microsoft.Authorization/roleDefinitions@2022-04-01' = {
  name: guid(inventoryRoleGuid, 'targeted-purge')
  properties: {
    roleName: 'ShareIARAG Console Index Purger'
    // docs/index (even delete-only batches) is authorized by documents/write; the console only sends delete actions.
    description: 'Read indexed file metadata with elevated read and delete chunks of deleted files through the docs/index API.'
    type: 'CustomRole'
    permissions: [
      {
        actions: []
        notActions: []
        dataActions: [
          'Microsoft.Search/searchServices/indexes/documents/read'
          'Microsoft.Search/searchServices/indexes/contentSecurity/elevatedOperations/read'
          'Microsoft.Search/searchServices/indexes/documents/delete'
          'Microsoft.Search/searchServices/indexes/documents/write'
        ]
        notDataActions: []
      }
    ]
    assignableScopes: [
      resourceGroup().id
    ]
  }
}

resource appPurger 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(searchResourceId, appName, 'targeted-purge')
  scope: search
  properties: {
    principalId: appPrincipalId
    roleDefinitionId: purgeRole.id
    principalType: 'ServicePrincipal'
  }
}

// Administrators run the console locally with their Azure CLI identity.
resource administratorPurgers 'Microsoft.Authorization/roleAssignments@2022-04-01' = [
  for administratorId in administratorObjectIds: {
    name: guid(searchResourceId, administratorId, 'targeted-purge')
    scope: search
    properties: {
      principalId: administratorId
      roleDefinitionId: purgeRole.id
      principalType: 'User'
    }
  }
]
