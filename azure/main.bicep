@description('Prefix for independently deployed Scheduling Orchestrator resources.')
@minLength(3)
@maxLength(24)
param namePrefix string

param location string = resourceGroup().location

@description('Immutable orchestrator container image, preferably pinned by sha256 digest.')
param containerImage string

@description('Optional ACR login server. Grant the managed identity AcrPull separately.')
param registryServer string = ''

@description('Existing Azure Container Apps environment resource ID.')
param containerAppsEnvironmentId string

@description('Location of the existing Azure Container Apps environment.')
param containerAppsLocation string = 'francecentral'

param keyVaultName string

param supabaseUrl string
param publicInterviewBaseUrl string
param googleOauthClientId string = ''
param googleOauthRedirectUri string = ''
param googleCalendarEnabled bool = true
param gmailEnabled bool = true
@description('Set false to deploy without Gemini intent parsing or its Key Vault secret.')
param geminiIntentEnabled bool = true
param geminiSchedulingModel string = 'gemini-3.8-flash'

@minValue(1)
@maxValue(120)
param geminiIntentTimeoutSeconds int = 30

param dataEncryptionKeyVersion string = 'v1'
@allowed([
  'staging'
  'production'
])
param environmentName string = 'production'
@minValue(1)
@maxValue(100)
param backendMaxParallelAiInterviews int = 1
@description('Local-time start used for date-only scheduling when HR supplies no window.')
@minLength(4)
@maxLength(8)
param defaultInterviewDayStart string = '09:00'
@description('Local-time end used for date-only scheduling when HR supplies no window.')
@minLength(4)
@maxLength(8)
param defaultInterviewDayEnd string = '18:00'
@minValue(1)
@maxValue(30)
param apiMinReplicas int = 1
@minValue(1)
@maxValue(30)
param apiMaxReplicas int = 5
@description('Fixed worker replica count; SKIP LOCKED leases make horizontal replicas safe.')
@minValue(1)
@maxValue(30)
param workerReplicas int = 1

resource keyVault 'Microsoft.KeyVault/vaults@2023-07-01' existing = {
  name: keyVaultName
}

resource identity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: '${namePrefix}-identity'
  location: location
}

resource keyVaultSecretsUser 'Microsoft.Authorization/roleDefinitions@2022-04-01' existing = {
  scope: subscription()
  name: '4633458b-17de-408a-b874-0445c86b69e6'
}

resource keyVaultRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(keyVault.id, identity.id, keyVaultSecretsUser.id)
  scope: keyVault
  properties: {
    principalId: identity.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: keyVaultSecretsUser.id
  }
}


var coreSecretDefinitions = [
  {
    name: 'supabase-db-url'
    keyVaultUrl: '${keyVault.properties.vaultUri}secrets/orchestrator-supabase-db-url'
    identity: identity.id
  }
  {
    name: 'data-encryption-key'
    keyVaultUrl: '${keyVault.properties.vaultUri}secrets/orchestrator-data-encryption-key'
    identity: identity.id
  }
]

var googleSecretDefinitions = (googleCalendarEnabled || gmailEnabled) ? [
  {
    name: 'google-oauth-client-secret'
    keyVaultUrl: '${keyVault.properties.vaultUri}secrets/orchestrator-google-oauth-client-secret'
    identity: identity.id
  }
] : []

var geminiSecretDefinitions = geminiIntentEnabled ? [
  {
    name: 'google-api-key'
    keyVaultUrl: '${keyVault.properties.vaultUri}secrets/orchestrator-google-api-key'
    identity: identity.id
  }
] : []

var secretDefinitions = concat(
  coreSecretDefinitions,
  googleSecretDefinitions,
  geminiSecretDefinitions
)

var baseEnvironment = [
  { name: 'ENVIRONMENT', value: environmentName }
  { name: 'LOG_LEVEL', value: 'INFO' }
  { name: 'SUPABASE_URL', value: supabaseUrl }
  { name: 'SUPABASE_AUTH_MODE', value: 'jwks' }
  { name: 'JWT_AUDIENCE', value: 'authenticated' }
  { name: 'PUBLIC_INTERVIEW_BASE_URL', value: publicInterviewBaseUrl }
  { name: 'BACKEND_MAX_PARALLEL_AI_INTERVIEWS', value: string(backendMaxParallelAiInterviews) }
  { name: 'DEFAULT_INTERVIEW_DAY_START', value: defaultInterviewDayStart }
  { name: 'DEFAULT_INTERVIEW_DAY_END', value: defaultInterviewDayEnd }
  { name: 'DATA_ENCRYPTION_KEY_VERSION', value: dataEncryptionKeyVersion }
  { name: 'GEMINI_SCHEDULING_MODEL', value: geminiSchedulingModel }
  { name: 'GEMINI_INTENT_TIMEOUT_SECONDS', value: string(geminiIntentTimeoutSeconds) }
  { name: 'GOOGLE_OAUTH_CLIENT_ID', value: googleOauthClientId }
  { name: 'GOOGLE_OAUTH_REDIRECT_URI', value: googleOauthRedirectUri }
  { name: 'GOOGLE_CALENDAR_ENABLED', value: string(googleCalendarEnabled) }
  { name: 'GMAIL_ENABLED', value: string(gmailEnabled) }
  { name: 'SUPABASE_DB_URL', secretRef: 'supabase-db-url' }
  { name: 'DATA_ENCRYPTION_KEY', secretRef: 'data-encryption-key' }
]

var googleEnvironment = (googleCalendarEnabled || gmailEnabled) ? [
  { name: 'GOOGLE_OAUTH_CLIENT_SECRET', secretRef: 'google-oauth-client-secret' }
] : []

var geminiEnvironment = geminiIntentEnabled ? [
  { name: 'GOOGLE_API_KEY', secretRef: 'google-api-key' }
] : []

var commonEnvironment = concat(
  baseEnvironment,
  googleEnvironment,
  geminiEnvironment
)

var registryConfiguration = empty(registryServer) ? [] : [
  {
    server: registryServer
    identity: identity.id
  }
]

resource api 'Microsoft.App/containerApps@2024-03-01' = {
  name: '${namePrefix}-api'
  location: containerAppsLocation
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${identity.id}': {}
    }
  }
  properties: {
    managedEnvironmentId: containerAppsEnvironmentId
    configuration: {
      activeRevisionsMode: 'Single'
      registries: registryConfiguration
      secrets: secretDefinitions
      ingress: {
        external: true
        allowInsecure: false
        targetPort: 8080
        transport: 'http'
        traffic: [
          { latestRevision: true, weight: 100 }
        ]
      }
    }
    template: {
      containers: [
        {
          name: 'orchestrator-api'
          image: containerImage
          command: ['talentflow-orchestrator-api']
          env: commonEnvironment
          resources: { cpu: json('0.5'), memory: '1Gi' }
          probes: [
            {
              type: 'Liveness'
              httpGet: { path: '/health', port: 8080, scheme: 'HTTP' }
              initialDelaySeconds: 10
              periodSeconds: 30
            }
            {
              type: 'Readiness'
              httpGet: { path: '/ready', port: 8080, scheme: 'HTTP' }
              initialDelaySeconds: 5
              periodSeconds: 10
            }
          ]
        }
      ]
      scale: {
        minReplicas: apiMinReplicas
        maxReplicas: apiMaxReplicas
        rules: [
          {
            name: 'http'
            http: { metadata: { concurrentRequests: '50' } }
          }
        ]
      }
    }
  }
  dependsOn: [keyVaultRole]
}

resource worker 'Microsoft.App/containerApps@2024-03-01' = {
  name: '${namePrefix}-worker'
  location: containerAppsLocation
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${identity.id}': {}
    }
  }
  properties: {
    managedEnvironmentId: containerAppsEnvironmentId
    configuration: {
      activeRevisionsMode: 'Single'
      registries: registryConfiguration
      secrets: secretDefinitions
    }
    template: {
      containers: [
        {
          name: 'orchestrator-worker'
          image: containerImage
          command: ['talentflow-orchestrator-worker']
          env: commonEnvironment
          resources: { cpu: json('0.5'), memory: '1Gi' }
        }
      ]
      scale: {
        minReplicas: workerReplicas
        maxReplicas: workerReplicas
      }
    }
  }
  dependsOn: [keyVaultRole]
}

output apiName string = api.name
output apiFqdn string = api.properties.configuration.ingress.fqdn
output workerName string = worker.name
output managedIdentityPrincipalId string = identity.properties.principalId
