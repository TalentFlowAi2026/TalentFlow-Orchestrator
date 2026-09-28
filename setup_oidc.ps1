$subscriptionId = "d97ab5d7-9c5a-4389-8883-50e219e598c2"
$tenantId = "ab992a61-6c45-48c8-acf1-eb356d9bea64"

$deploymentResourceGroup = "talentflow-orchestrator-prod-rg"
$identityName = "talentflow-orchestrator-github"

$acrId = "/subscriptions/d97ab5d7-9c5a-4389-8883-50e219e598c2/resourceGroups/talent-flow-ai_group/providers/Microsoft.ContainerRegistry/registries/talentflowai2026acr"

$repo = "TalentFlowAi2026/TalentFlow-Orchestrator"

az account set `
  --subscription $subscriptionId

az identity create `
  --name $identityName `
  --resource-group $deploymentResourceGroup

$clientId = az identity show `
  --name $identityName `
  --resource-group $deploymentResourceGroup `
  --query clientId `
  -o tsv

$principalId = az identity show `
  --name $identityName `
  --resource-group $deploymentResourceGroup `
  --query principalId `
  -o tsv

az identity federated-credential create `
  --name github-main `
  --identity-name $identityName `
  --resource-group $deploymentResourceGroup `
  --issuer https://token.actions.githubusercontent.com `
  --subject "repo:$repo`:ref:refs/heads/main" `
  --audiences api://AzureADTokenExchange

az role assignment create `
  --assignee-object-id $principalId `
  --assignee-principal-type ServicePrincipal `
  --role Contributor `
  --scope "/subscriptions/$subscriptionId/resourceGroups/$deploymentResourceGroup"

az role assignment create `
  --assignee-object-id $principalId `
  --assignee-principal-type ServicePrincipal `
  --role "Container Registry Tasks Contributor" `
  --scope $acrId

Write-Host ""
Write-Host "Create these GitHub repository secrets:"
Write-Host "AZURE_CLIENT_ID=$clientId"
Write-Host "AZURE_TENANT_ID=$tenantId"
Write-Host "AZURE_SUBSCRIPTION_ID=$subscriptionId"