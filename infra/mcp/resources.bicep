param location string
param tags object
param planName string
param appName string
param appInsightsName string
param keyVaultName string
param searchName string
param workspaceId string
param tenantId string
param entraClientId string
param tokenAudiences string
param allowedClientIds string
param requiredScope string
param identityName string
param deployerObjectId string
param searchReadRoleId string

resource search 'Microsoft.Search/searchServices@2025-05-01' existing = {
  name: searchName
}

resource vault 'Microsoft.KeyVault/vaults@2023-07-01' existing = {
  name: keyVaultName
}

// The Entra app trusts this identity with a federated identity credential
// (issuer https://login.microsoftonline.com/<tenant>/v2.0, subject = principalId,
// audience api://AzureADTokenExchange), created with `az ad app federated-credential create`.
resource oboIdentity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: identityName
  location: location
  tags: tags
}

resource plan 'Microsoft.Web/serverfarms@2024-11-01' = {
  name: planName
  location: location
  tags: tags
  kind: 'linux'
  // Shared with the RAG Control console (infra/main.bicep).
  sku: {
    name: 'B2'
    tier: 'Basic'
    capacity: 1
  }
  properties: {
    reserved: true
  }
}

resource insights 'Microsoft.Insights/components@2020-02-02' = {
  name: appInsightsName
  location: location
  kind: 'web'
  tags: tags
  properties: {
    Application_Type: 'web'
    WorkspaceResourceId: workspaceId
  }
}

resource app 'Microsoft.Web/sites@2024-11-01' = {
  name: appName
  location: location
  kind: 'app,linux'
  tags: tags
  identity: {
    type: 'SystemAssigned, UserAssigned'
    userAssignedIdentities: {
      '${oboIdentity.id}': {}
    }
  }
  properties: {
    serverFarmId: plan.id
    httpsOnly: true
    siteConfig: {
      alwaysOn: true
      linuxFxVersion: 'PYTHON|3.11'
      minTlsVersion: '1.2'
      ftpsState: 'Disabled'
      healthCheckPath: '/healthz'
      http20Enabled: true
      appCommandLine: 'python -m uvicorn cloud_app:app --host 0.0.0.0 --port 8000'
    }
  }
}

resource settings 'Microsoft.Web/sites/config@2024-11-01' = {
  parent: app
  name: 'appsettings'
  properties: {
    SCM_DO_BUILD_DURING_DEPLOYMENT: 'true'
    ENABLE_ORYX_BUILD: 'true'
    ORYX_DISABLE_COMPRESSION: 'true'
    WEBSITES_CONTAINER_START_TIME_LIMIT: '1800'
    MCP_MODE: 'hosted'
    SEARCH_ENDPOINT: 'https://${searchName}.search.windows.net'
    MCP_PUBLIC_HOST: app.properties.defaultHostName
    AZURE_TENANT_ID: tenantId
    MCP_CLIENT_ID: entraClientId
    MCP_FIC_IDENTITY_CLIENT_ID: oboIdentity.properties.clientId
    MCP_TOKEN_AUDIENCES: tokenAudiences
    MCP_ALLOWED_CLIENT_IDS: allowedClientIds
    MCP_REQUIRED_SCOPE: requiredScope
    APPLICATIONINSIGHTS_CONNECTION_STRING: insights.properties.ConnectionString
  }
}

resource scmPolicy 'Microsoft.Web/sites/basicPublishingCredentialsPolicies@2024-11-01' = {
  parent: app
  name: 'scm'
  properties: {
    allow: false
  }
}

resource ftpPolicy 'Microsoft.Web/sites/basicPublishingCredentialsPolicies@2024-11-01' = {
  parent: app
  name: 'ftp'
  properties: {
    allow: false
  }
}

resource vaultDeployer 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(vault.id, deployerObjectId, 'b86a8fe4-44ce-4948-aee5-eccb2c155cd7')
  scope: vault
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', 'b86a8fe4-44ce-4948-aee5-eccb2c155cd7')
    principalId: deployerObjectId
    principalType: 'User'
  }
}

resource searchReader 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(search.id, app.id, searchReadRoleId)
  scope: search
  properties: {
    roleDefinitionId: searchReadRoleId
    principalId: app.identity.principalId
    principalType: 'ServicePrincipal'
  }
}

output appId string = app.id
output hostname string = app.properties.defaultHostName
output oboIdentityPrincipalId string = oboIdentity.properties.principalId
