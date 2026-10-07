<#
.SYNOPSIS
  Step 1 of 2: registers the Entra app for m365-carddav (certificate auth).

.DESCRIPTION
  Creates an app registration + service principal and uploads the public
  certificate created by `gen-cert`.

  IMPORTANT: NO API permission (e.g. Contacts.Read) is granted in Entra.
  An Entra admin consent would give the app access to EVERY mailbox in the
  tenant. Access is granted exclusively in step 2 via Exchange Online
  RBAC for Applications, scoped to a single mailbox.

  Required role: Application Administrator (or Global Administrator).
  Module: Microsoft.Graph.Applications (same version as Microsoft.Graph.Authentication!)

.EXAMPLE
  ./1-Register-EntraApp.ps1 -CertPath ./graph-cert.cer
#>
param(
    [Parameter(Mandatory)] [string] $CertPath,
    [string] $Name = "m365-carddav"
)
$ErrorActionPreference = "Stop"

# Load Applications first, so it pulls its matching Authentication assembly.
Import-Module Microsoft.Graph.Applications
Connect-MgGraph -Scopes "Application.ReadWrite.All" -NoWelcome

$existing = Get-MgApplication -Filter "displayName eq '$Name'"
if ($existing) {
    throw "An app named '$Name' already exists (AppId $($existing.AppId -join ', ')). " +
          "Use it for step 2, remove it first, or pass a different -Name."
}

$certBytes = [System.IO.File]::ReadAllBytes((Resolve-Path $CertPath))
$app = New-MgApplication -DisplayName $Name -SignInAudience "AzureADMyOrg" -KeyCredentials @(@{
    Type = "AsymmetricX509Cert"; Usage = "Verify"; Key = $certBytes; DisplayName = "$Name-cert"
})
$sp = New-MgServicePrincipal -AppId $app.AppId

Write-Host ""
Write-Host "TENANT_ID          = $((Get-MgContext).TenantId)"
Write-Host "CLIENT_ID (AppId)  = $($app.AppId)"
Write-Host "ServicePrincipalId = $($sp.Id)"
Write-Host ""
Write-Host "Next: ./2-Grant-MailboxAccess.ps1 -AppId $($app.AppId) -ServicePrincipalId $($sp.Id) -Mailbox <user@domain>"
