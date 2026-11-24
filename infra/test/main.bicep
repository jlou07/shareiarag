targetScope = 'subscription'

@description('Short unique suffix (3-8 lowercase letters or digits) appended to every resource name. Use a new one per environment.')
@minLength(3)
@maxLength(8)
param suffix string

@description('Name of the dedicated resource group created for this environment.')
param resourceGroupName string

param location string = 'francecentral'
param environmentName string = 'shareiarag-test-${suffix}'
param sessionId string = ''
param deployedBy string = ''
param createdAt string = utcNow()

@description('Object ID of the signed-in user who prepares the test data and secrets.')
@minLength(36)
@maxLength(36)
param deployerObjectId string

@description('User object IDs authorized to administer the test console.')
@minLength(1)
param administratorObjectIds array

@description('Two distinct non-administrator user object IDs for isolated agent access tests.')
@minLength(2)
@maxLength(2)
param testUserObjectIds array

@description('Client IDs of the three separate test Entra applications.')
@minLength(36)
@maxLength(36)
param consoleClientId string
@minLength(36)
@maxLength(36)
param downloadClientId string
@minLength(36)
@maxLength(36)
param mcpClientId string
@description('Optional Application ID URI of the Teams Developer Portal SSO registration. Leave empty until the Microsoft 365 connector is set up.')
param mcpSsoAudience string = ''

@description('Existing security groups used for the two synthetic shares; memberships are not changed.')
@minLength(36)
@maxLength(36)
param groupAObjectId string
@minLength(36)
@maxLength(36)
param groupBObjectId string

@description('Set to true only after both test secrets have been created in the test vault.')
param grantSecretAccess bool = false

@description('Settings added by the console when sources are connected; deploy_test_env.py reads them from the live apps so a redeployment keeps them.')
param downloadShareGroups string = ''
param downloadExtraSettings object = {}
param consoleExtraSettings object = {}

var searchName = 'srch-shareiarag-test-${suffix}'
var foundryAccountName = 'aif-shareiarag-test-${suffix}'
var foundryProjectName = 'proj-shareiarag-test-${suffix}'
var filesAccountName = 'stshragfiles${suffix}'
var stateAccountName = 'stshragstate${suffix}'
var webPlanName = 'plan-shareiarag-test-${suffix}'
var consoleAppName = 'app-shareiarag-test-console-${suffix}'
var mcpAppName = 'app-shareiarag-test-mcp-${suffix}'
var functionPlanName = 'plan-shareiarag-test-func-${suffix}'
var downloadAppName = 'func-shareiarag-test-dl-${suffix}'
var vaultName = 'kv-shrag-test-${suffix}'
var mcpIdentityName = 'id-shareiarag-test-mcp-${suffix}'
var logName = 'log-shareiarag-test-${suffix}'
var functionInsightsName = 'appi-shareiarag-test-dl-${suffix}'
var mcpInsightsName = 'appi-shareiarag-test-mcp-${suffix}'

var tags = union({
  'created-at': createdAt
  environment: environmentName
  'deployed-by': deployedBy
}, empty(sessionId) ? {} : {
  'app-onboard-skill': 'true'
  'app-onboard-session-id': sessionId
})

resource testResourceGroup 'Microsoft.Resources/resourceGroups@2024-11-01' = {
  name: resourceGroupName
  location: location
  tags: tags
}

module resources './modules/resources.bicep' = {
  name: 'shareiarag-test-resources-${suffix}'
  scope: testResourceGroup
  params: {
    suffix: suffix
    location: location
    tags: tags
    tenantId: subscription().tenantId
    administratorObjectIds: administratorObjectIds
    consoleClientId: consoleClientId
    downloadClientId: downloadClientId
    mcpClientId: mcpClientId
    mcpSsoAudience: mcpSsoAudience
    groupAObjectId: groupAObjectId
    groupBObjectId: groupBObjectId
    downloadShareGroups: downloadShareGroups
    downloadExtraSettings: downloadExtraSettings
    consoleExtraSettings: consoleExtraSettings
    searchName: searchName
    foundryAccountName: foundryAccountName
    foundryProjectName: foundryProjectName
    filesAccountName: filesAccountName
    stateAccountName: stateAccountName
    webPlanName: webPlanName
    consoleAppName: consoleAppName
    mcpAppName: mcpAppName
    functionPlanName: functionPlanName
    downloadAppName: downloadAppName
    vaultName: vaultName
    mcpIdentityName: mcpIdentityName
    logName: logName
    functionInsightsName: functionInsightsName
    mcpInsightsName: mcpInsightsName
  }
}

module roles './modules/roles.bicep' = {
  name: 'shareiarag-test-roles-${suffix}'
  scope: testResourceGroup
  params: {
    suffix: suffix
    deployerObjectId: deployerObjectId
    administratorObjectIds: administratorObjectIds
    testUserObjectIds: testUserObjectIds
    searchName: searchName
    foundryAccountName: foundryAccountName
    foundryProjectName: foundryProjectName
    filesAccountName: filesAccountName
    stateAccountName: stateAccountName
    consoleAppName: consoleAppName
    mcpAppName: mcpAppName
    downloadAppName: downloadAppName
    vaultName: vaultName
  }
  dependsOn: [
    resources
  ]
}

module secretPermissions './modules/secret-permissions.bicep' = if (grantSecretAccess) {
  name: 'shareiarag-test-secret-permissions-${suffix}'
  scope: testResourceGroup
  params: {
    vaultName: vaultName
    filesAccountName: filesAccountName
    consoleAppName: consoleAppName
    downloadAppName: downloadAppName
  }
  dependsOn: [
    resources
    roles
  ]
}

output resourceGroupId string = testResourceGroup.id
output consoleUrl string = resources.outputs.consoleUrl
output mcpUrl string = resources.outputs.mcpUrl
output downloadUrl string = resources.outputs.downloadUrl
output mcpFederatedIdentityPrincipalId string = resources.outputs.mcpFederatedIdentityPrincipalId
output names object = {
  resourceGroup: resourceGroupName
  search: searchName
  foundryAccount: foundryAccountName
  foundryProject: foundryProjectName
  filesAccount: filesAccountName
  stateAccount: stateAccountName
  consoleApp: consoleAppName
  mcpApp: mcpAppName
  downloadApp: downloadAppName
  vault: vaultName
}
