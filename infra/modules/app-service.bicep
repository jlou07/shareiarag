param location string
param appName string
param hostingPlanId string
param subscriptionId string
param resourceGroupName string
param tenantId string
param entraClientId string
param adminObjectIds string
param searchServiceName string
param searchEndpoint string
param stateAccountName string
param stateContainerName string
param downloadAppName string
param keyVaultName string
param filesAccountName string
param groupDirectoryEnabled bool

resource app 'Microsoft.Web/sites@2026-08-01' = {
  name: appName
  location: location
  kind: 'app,linux'
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    serverFarmId: hostingPlanId
    httpsOnly: true
    siteConfig: {
      alwaysOn: true
      linuxFxVersion: 'PYTHON|3.11'
      minTlsVersion: '1.2'
      ftpsState: 'Disabled'
      healthCheckPath: '/healthz'
      http20Enabled: true
      appCommandLine: 'gunicorn --bind=0.0.0.0:8000 app:app'
      appSettings: [
        { name: 'SCM_DO_BUILD_DURING_DEPLOYMENT', value: 'true' }
        { name: 'ENABLE_ORYX_BUILD', value: 'true' }
        { name: 'ORYX_DISABLE_COMPRESSION', value: 'true' }
        { name: 'WEBSITES_CONTAINER_START_TIME_LIMIT', value: '1800' }
        { name: 'AZURE_SUBSCRIPTION_ID', value: subscriptionId }
        { name: 'AZURE_RESOURCE_GROUP', value: resourceGroupName }
        { name: 'AZURE_TENANT_ID', value: tenantId }
        { name: 'ENTRA_CLIENT_ID', value: entraClientId }
        { name: 'ADMIN_OBJECT_IDS', value: adminObjectIds }
        { name: 'AUTH_MODE', value: 'entra' }
        { name: 'SEARCH_SERVICE_NAME', value: searchServiceName }
        { name: 'SEARCH_ENDPOINT', value: searchEndpoint }
        { name: 'SEARCH_API_VERSION', value: '2026-08-01-preview' }
        { name: 'KNOWLEDGE_SOURCE_NAME', value: 'af-ks-all' }
        { name: 'KNOWLEDGE_BASE_NAME', value: 'af-kb-all' }
        { name: 'FILES_ACCOUNT_NAME', value: filesAccountName }
        { name: 'KEY_VAULT_NAME', value: keyVaultName }
        { name: 'FILES_CONNECTION_${toUpper(filesAccountName)}', value: '@Microsoft.KeyVault(VaultName=${keyVaultName};SecretName=files-connection-${filesAccountName})' }
        { name: 'FOUNDRY_ACCOUNT_NAME', value: 'aif-shareiarag' }
        { name: 'DOWNLOAD_APP_NAME', value: downloadAppName }
        { name: 'STATE_ACCOUNT_NAME', value: stateAccountName }
        { name: 'STATE_CONTAINER_NAME', value: stateContainerName }
        { name: 'HOSTING_PLAN_ID', value: hostingPlanId }
        { name: 'GROUP_DIRECTORY_ENABLED', value: string(groupDirectoryEnabled) }
        { name: 'GUNICORN_CMD_ARGS', value: '--workers 1 --threads 4 --timeout 120' }
      ]
    }
  }
}

resource containerLogs 'Microsoft.Web/sites/config@2024-11-01' = {
  parent: app
  name: 'logs'
  properties: {
    httpLogs: {
      fileSystem: {
        enabled: true
        retentionInDays: 3
        retentionInMb: 100
      }
    }
  }
}

resource scmPolicy 'Microsoft.Web/sites/basicPublishingCredentialsPolicies@2024-11-01' = {
  parent: app
  name: 'scm'
  properties: {
    allow: false
  }
}

output principalId string = app.identity.principalId
output resourceId string = app.id
