@description('Existing download Function app name.')
param functionAppName string

@description('Entra tenant ID.')
param tenantId string

@description('Entra application (client) ID used by App Service Authentication on the Function.')
param clientId string

resource downloadFunction 'Microsoft.Web/sites@2024-11-01' existing = {
  name: functionAppName
}

// function_app.py trusts x-ms-client-principal only while this platform authentication is enabled.
resource auth 'Microsoft.Web/sites/config@2024-11-01' = {
  parent: downloadFunction
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
          clientId: clientId
          clientSecretSettingName: 'MICROSOFT_PROVIDER_AUTHENTICATION_SECRET'
          openIdIssuer: '${environment().authentication.loginEndpoint}${tenantId}/v2.0'
        }
        validation: {
          allowedAudiences: [
            'api://${clientId}'
            clientId
          ]
        }
      }
      apple: { enabled: false }
      facebook: { enabled: false }
      gitHub: { enabled: false }
      google: { enabled: false }
      legacyMicrosoftAccount: { enabled: false }
      twitter: { enabled: false }
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
