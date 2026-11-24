param suffix string
param location string
param tags object
param tenantId string
param administratorObjectIds array
param consoleClientId string
param downloadClientId string
param mcpClientId string
param mcpSsoAudience string
param groupAObjectId string
param groupBObjectId string
param downloadShareGroups string
param downloadExtraSettings object
param consoleExtraSettings object
param searchName string
param foundryAccountName string
param foundryProjectName string
param filesAccountName string
param stateAccountName string
param webPlanName string
param consoleAppName string
param mcpAppName string
param functionPlanName string
param downloadAppName string
param vaultName string
param mcpIdentityName string
param logName string
param functionInsightsName string
param mcpInsightsName string

// Microsoft Enterprise Token Store: the well-known client used by Microsoft 365 Copilot connectors for SSO.
var m365TokenStoreClientId = 'ab3be6b7-f5df-413d-ac2d-abf1e3fd9c0b'
var m365SsoEnabled = !empty(mcpSsoAudience)
var consoleExtraAppSettings = [for item in items(consoleExtraSettings): { name: item.key, value: item.value }]
var downloadExtraAppSettings = [for item in items(downloadExtraSettings): { name: item.key, value: item.value }]

resource log 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: logName
  location: location
  tags: tags
  properties: {
    sku: {
      name: 'PerGB2018'
    }
    retentionInDays: 30
  }
}

resource functionInsights 'Microsoft.Insights/components@2020-02-02' = {
  name: functionInsightsName
  location: location
  kind: 'web'
  tags: tags
  properties: {
    Application_Type: 'web'
    WorkspaceResourceId: log.id
  }
}

resource mcpInsights 'Microsoft.Insights/components@2020-02-02' = {
  name: mcpInsightsName
  location: location
  kind: 'web'
  tags: tags
  properties: {
    Application_Type: 'web'
    WorkspaceResourceId: log.id
  }
}

resource files 'Microsoft.Storage/storageAccounts@2025-01-01' = {
  name: filesAccountName
  location: location
  kind: 'FileStorage'
  sku: {
    name: 'StandardV2_LRS'
  }
  tags: tags
  properties: {
    supportsHttpsTrafficOnly: true
    minimumTlsVersion: 'TLS1_2'
    allowSharedKeyAccess: true
    allowBlobPublicAccess: false
  }
}

resource fileService 'Microsoft.Storage/storageAccounts/fileServices@2025-01-01' = {
  parent: files
  name: 'default'
}

resource groupAShare 'Microsoft.Storage/storageAccounts/fileServices/shares@2025-01-01' = {
  parent: fileService
  name: 'admin-share'
  properties: {
    enabledProtocols: 'SMB'
    shareQuota: 32
    provisionedIops: 500
    provisionedBandwidthMibps: 60
  }
}

resource groupBShare 'Microsoft.Storage/storageAccounts/fileServices/shares@2025-01-01' = {
  parent: fileService
  name: 'alluser-share'
  properties: {
    enabledProtocols: 'SMB'
    shareQuota: 32
    provisionedIops: 500
    provisionedBandwidthMibps: 60
  }
}

resource state 'Microsoft.Storage/storageAccounts@2025-01-01' = {
  name: stateAccountName
  location: location
  kind: 'StorageV2'
  sku: {
    name: 'Standard_LRS'
  }
  tags: tags
  properties: {
    supportsHttpsTrafficOnly: true
    minimumTlsVersion: 'TLS1_2'
    allowSharedKeyAccess: false
    allowBlobPublicAccess: false
  }
}

resource blobs 'Microsoft.Storage/storageAccounts/blobServices@2025-01-01' = {
  parent: state
  name: 'default'
}

resource consoleContainer 'Microsoft.Storage/storageAccounts/blobServices/containers@2025-01-01' = {
  parent: blobs
  name: 'rag-console-test-${suffix}'
  properties: {
    publicAccess: 'None'
  }
}

resource functionContainer 'Microsoft.Storage/storageAccounts/blobServices/containers@2025-01-01' = {
  parent: blobs
  name: 'function-packages'
  properties: {
    publicAccess: 'None'
  }
}

resource vault 'Microsoft.KeyVault/vaults@2024-11-01' = {
  name: vaultName
  location: location
  tags: tags
  properties: {
    tenantId: tenantId
    sku: {
      family: 'A'
      name: 'standard'
    }
    enableRbacAuthorization: true
    enableSoftDelete: true
    softDeleteRetentionInDays: 7
    publicNetworkAccess: 'Enabled'
  }
}

resource search 'Microsoft.Search/searchServices@2025-05-01' = {
  name: searchName
  location: location
  tags: tags
  identity: {
    type: 'SystemAssigned'
  }
  sku: {
    name: 'standard'
  }
  properties: {
    replicaCount: 1
    partitionCount: 1
    semanticSearch: 'free'
    disableLocalAuth: true
    publicNetworkAccess: 'Enabled'
  }
}

resource foundry 'Microsoft.CognitiveServices/accounts@2025-06-01' = {
  name: foundryAccountName
  location: location
  kind: 'AIServices'
  sku: {
    name: 'S0'
  }
  tags: tags
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    customSubDomainName: foundryAccountName
    allowProjectManagement: true
    disableLocalAuth: true
    publicNetworkAccess: 'Enabled'
  }
}

resource foundryProject 'Microsoft.CognitiveServices/accounts/projects@2025-06-01' = {
  parent: foundry
  name: foundryProjectName
  location: location
  tags: tags
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    displayName: foundryProjectName
  }
}

resource embeddingModel 'Microsoft.CognitiveServices/accounts/deployments@2025-06-01' = {
  parent: foundry
  name: 'text-embedding-3-small'
  dependsOn: [
    foundryProject
  ]
  sku: {
    name: 'GlobalStandard'
    capacity: 150
  }
  properties: {
    model: {
      format: 'OpenAI'
      name: 'text-embedding-3-small'
      version: '1'
    }
  }
}

resource chatModel 'Microsoft.CognitiveServices/accounts/deployments@2025-06-01' = {
  parent: foundry
  name: 'gpt-5-mini'
  dependsOn: [
    embeddingModel
  ]
  sku: {
    name: 'GlobalStandard'
    capacity: 500
  }
  properties: {
    model: {
      format: 'OpenAI'
      name: 'gpt-5-mini'
      version: '2025-08-07'
    }
  }
}

resource mcpIdentity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: mcpIdentityName
  location: location
  tags: tags
}

resource webPlan 'Microsoft.Web/serverfarms@2024-11-01' = {
  name: webPlanName
  location: location
  kind: 'linux'
  tags: tags
  sku: {
    name: 'B2'
    tier: 'Basic'
    capacity: 1
  }
  properties: {
    reserved: true
  }
}

resource functionPlan 'Microsoft.Web/serverfarms@2024-11-01' = {
  name: functionPlanName
  location: location
  kind: 'functionapp'
  tags: tags
  sku: {
    name: 'FC1'
    tier: 'FlexConsumption'
  }
  properties: {
    reserved: true
  }
}

resource consoleApp 'Microsoft.Web/sites@2024-11-01' = {
  name: consoleAppName
  location: location
  kind: 'app,linux'
  tags: tags
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    serverFarmId: webPlan.id
    httpsOnly: true
    siteConfig: {
      alwaysOn: true
      linuxFxVersion: 'PYTHON|3.11'
      minTlsVersion: '1.2'
      ftpsState: 'Disabled'
      healthCheckPath: '/healthz'
      http20Enabled: true
      appCommandLine: 'gunicorn --bind=0.0.0.0:8000 app:app'
      appSettings: concat([
        { name: 'SCM_DO_BUILD_DURING_DEPLOYMENT', value: 'true' }
        { name: 'ENABLE_ORYX_BUILD', value: 'true' }
        { name: 'ORYX_DISABLE_COMPRESSION', value: 'true' }
        { name: 'WEBSITES_CONTAINER_START_TIME_LIMIT', value: '1800' }
        { name: 'GUNICORN_CMD_ARGS', value: '--workers 1 --threads 4 --timeout 120' }
        { name: 'AZURE_SUBSCRIPTION_ID', value: subscription().subscriptionId }
        { name: 'AZURE_RESOURCE_GROUP', value: resourceGroup().name }
        { name: 'AZURE_TENANT_ID', value: tenantId }
        { name: 'ENTRA_CLIENT_ID', value: consoleClientId }
        { name: 'ADMIN_OBJECT_IDS', value: join(administratorObjectIds, ',') }
        { name: 'AUTH_MODE', value: 'entra' }
        { name: 'SEARCH_SERVICE_NAME', value: search.name }
        { name: 'SEARCH_ENDPOINT', value: 'https://${search.name}.search.windows.net' }
        { name: 'SEARCH_API_VERSION', value: '2026-08-01-preview' }
        { name: 'KNOWLEDGE_SOURCE_NAME', value: 'af-ks-all' }
        { name: 'KNOWLEDGE_BASE_NAME', value: 'af-kb-all' }
        { name: 'FILES_ACCOUNT_NAME', value: files.name }
        { name: 'FILES_CONNECTION_${toUpper(files.name)}', value: '@Microsoft.KeyVault(VaultName=${vault.name};SecretName=files-connection-${files.name})' }
        { name: 'KEY_VAULT_NAME', value: vault.name }
        { name: 'FOUNDRY_ACCOUNT_NAME', value: foundry.name }
        { name: 'FOUNDRY_PROJECT_NAME', value: foundryProject.name }
        { name: 'FOUNDRY_AGENT_NAME', value: 'agent-shareiarag-test' }
        { name: 'DOWNLOAD_APP_NAME', value: downloadAppName }
        { name: 'STATE_ACCOUNT_NAME', value: state.name }
        { name: 'STATE_CONTAINER_NAME', value: consoleContainer.name }
        { name: 'MCP_APP_NAME', value: mcpAppName }
        { name: 'HOSTING_PLAN_ID', value: webPlan.id }
        // deploy_test_env.py grants Group.ReadBasic.All to the console identity (entra-post step).
        { name: 'GROUP_DIRECTORY_ENABLED', value: 'true' }
      ], consoleExtraAppSettings)
    }
  }
}

resource mcpApp 'Microsoft.Web/sites@2024-11-01' = {
  name: mcpAppName
  location: location
  kind: 'app,linux'
  tags: tags
  identity: {
    type: 'SystemAssigned, UserAssigned'
    userAssignedIdentities: {
      '${mcpIdentity.id}': {}
    }
  }
  properties: {
    serverFarmId: webPlan.id
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

resource downloadApp 'Microsoft.Web/sites@2024-11-01' = {
  name: downloadAppName
  location: location
  kind: 'functionapp,linux'
  tags: tags
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    serverFarmId: functionPlan.id
    httpsOnly: true
    functionAppConfig: {
      deployment: {
        storage: {
          type: 'blobContainer'
          value: '${state.properties.primaryEndpoints.blob}${functionContainer.name}'
          authentication: {
            type: 'SystemAssignedIdentity'
          }
        }
      }
      runtime: {
        name: 'python'
        version: '3.11'
      }
      scaleAndConcurrency: {
        maximumInstanceCount: 100
        instanceMemoryMB: 2048
        alwaysReady: []
      }
    }
    siteConfig: {
      minTlsVersion: '1.2'
      ftpsState: 'Disabled'
      appSettings: concat([
        { name: 'AzureWebJobsStorage__accountName', value: state.name }
        { name: 'APPLICATIONINSIGHTS_CONNECTION_STRING', value: functionInsights.properties.ConnectionString }
        { name: 'FILES_ACCOUNT_NAME', value: files.name }
        { name: 'FILES_ACCOUNT_KEY', value: '@Microsoft.KeyVault(VaultName=${vault.name};SecretName=files-connection-${files.name})' }
        { name: 'SHARE_GROUPS', value: empty(downloadShareGroups) ? 'admin-share=${groupAObjectId};alluser-share=${groupBObjectId}' : downloadShareGroups }
        { name: 'MICROSOFT_PROVIDER_AUTHENTICATION_SECRET', value: '@Microsoft.KeyVault(VaultName=${vault.name};SecretName=download-provider-secret)' }
        // The published download-func supports multiple accounts; the console requires this marker to add sources.
        { name: 'MULTI_ACCOUNT_SOURCES_VERSION', value: '1' }
      ], downloadExtraAppSettings)
    }
  }
}

resource mcpSettings 'Microsoft.Web/sites/config@2024-11-01' = {
  parent: mcpApp
  name: 'appsettings'
  properties: {
    SCM_DO_BUILD_DURING_DEPLOYMENT: 'true'
    ENABLE_ORYX_BUILD: 'true'
    ORYX_DISABLE_COMPRESSION: 'true'
    WEBSITES_CONTAINER_START_TIME_LIMIT: '1800'
    MCP_MODE: 'hosted'
    SEARCH_ENDPOINT: 'https://${search.name}.search.windows.net'
    KB_NAME: 'af-kb-all'
    KS_NAME: 'af-ks-all'
    DOWNLOAD_BASE: 'https://${downloadApp.properties.defaultHostName}/api/f'
    DEFAULT_STORAGE_ACCOUNT: files.name
    MCP_PUBLIC_HOST: mcpApp.properties.defaultHostName
    AZURE_TENANT_ID: tenantId
    MCP_CLIENT_ID: mcpClientId
    MCP_FIC_IDENTITY_CLIENT_ID: mcpIdentity.properties.clientId
    MCP_TOKEN_AUDIENCES: m365SsoEnabled ? '${mcpClientId},api://${mcpClientId},${mcpSsoAudience}' : '${mcpClientId},api://${mcpClientId}'
    MCP_ALLOWED_CLIENT_IDS: m365SsoEnabled ? '${consoleClientId},${m365TokenStoreClientId}' : consoleClientId
    MCP_REQUIRED_SCOPE: 'access_as_user'
    APPLICATIONINSIGHTS_CONNECTION_STRING: mcpInsights.properties.ConnectionString
  }
}

resource downloadAuth 'Microsoft.Web/sites/config@2024-11-01' = {
  parent: downloadApp
  name: 'authsettingsV2'
  properties: {
    platform: {
      enabled: true
      runtimeVersion: '~1'
    }
    globalValidation: {
      requireAuthentication: true
      unauthenticatedClientAction: 'RedirectToLoginPage'
      redirectToProvider: 'azureactivedirectory'
    }
    httpSettings: {
      requireHttps: true
      forwardProxy: {
        convention: 'NoProxy'
      }
      routes: {
        apiPrefix: '/.auth'
      }
    }
    identityProviders: {
      azureActiveDirectory: {
        enabled: true
        registration: {
          clientId: downloadClientId
          clientSecretSettingName: 'MICROSOFT_PROVIDER_AUTHENTICATION_SECRET'
          openIdIssuer: '${environment().authentication.loginEndpoint}${tenantId}/v2.0'
        }
        validation: {
          allowedAudiences: [
            downloadClientId
            'api://${downloadClientId}'
          ]
        }
      }
    }
    login: {
      cookieExpiration: {
        convention: 'FixedTime'
        timeToExpiration: '08:00:00'
      }
      nonce: {
        validateNonce: true
        nonceExpirationInterval: '00:05:00'
      }
      preserveUrlFragmentsForLogins: false
      tokenStore: {
        enabled: true
        tokenRefreshExtensionHours: 72
      }
    }
  }
}

resource consoleScm 'Microsoft.Web/sites/basicPublishingCredentialsPolicies@2024-11-01' = {
  parent: consoleApp
  name: 'scm'
  properties: {
    allow: false
  }
}
resource consoleFtp 'Microsoft.Web/sites/basicPublishingCredentialsPolicies@2024-11-01' = {
  parent: consoleApp
  name: 'ftp'
  properties: {
    allow: false
  }
}
resource mcpScm 'Microsoft.Web/sites/basicPublishingCredentialsPolicies@2024-11-01' = {
  parent: mcpApp
  name: 'scm'
  properties: {
    allow: false
  }
}
resource mcpFtp 'Microsoft.Web/sites/basicPublishingCredentialsPolicies@2024-11-01' = {
  parent: mcpApp
  name: 'ftp'
  properties: {
    allow: false
  }
}
resource downloadScm 'Microsoft.Web/sites/basicPublishingCredentialsPolicies@2024-11-01' = {
  parent: downloadApp
  name: 'scm'
  properties: {
    allow: false
  }
}
resource downloadFtp 'Microsoft.Web/sites/basicPublishingCredentialsPolicies@2024-11-01' = {
  parent: downloadApp
  name: 'ftp'
  properties: {
    allow: false
  }
}

output consoleUrl string = 'https://${consoleApp.properties.defaultHostName}'
output mcpUrl string = 'https://${mcpApp.properties.defaultHostName}/mcp'
output downloadUrl string = 'https://${downloadApp.properties.defaultHostName}/api/f'
output mcpFederatedIdentityPrincipalId string = mcpIdentity.properties.principalId
