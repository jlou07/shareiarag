param appPrincipalId string
param appName string
param planName string

resource plan 'Microsoft.Web/serverfarms@2022-03-01' existing = {
  name: planName
}

resource readerAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(plan.id, appName, 'acdd72a7-3385-48ef-bd42-f606fba81ae7')
  scope: plan
  properties: {
    principalId: appPrincipalId
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', 'acdd72a7-3385-48ef-bd42-f606fba81ae7')
    principalType: 'ServicePrincipal'
  }
}
