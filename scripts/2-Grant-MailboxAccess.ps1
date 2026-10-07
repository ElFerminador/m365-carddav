<#
.SYNOPSIS
  Step 2 of 2: grants the app read access to the contacts of ONE mailbox.

.DESCRIPTION
  Uses Exchange Online "RBAC for Applications":
    - registers the service principal in Exchange Online
    - creates a management scope that matches exactly one mailbox
    - assigns the role "Application Contacts.Read" limited to that scope

  Required role: Exchange Administrator / Organization Management.
  A normal user account does not even see the cmdlets used here
  (New-ServicePrincipal "is not recognized"). Pass -AdminUpn to sign in
  with the admin account.

.EXAMPLE
  ./2-Grant-MailboxAccess.ps1 -AppId <guid> -ServicePrincipalId <guid> `
      -Mailbox user@contoso.com -AdminUpn admin@contoso.onmicrosoft.com
#>
param(
    [Parameter(Mandatory)] [string] $AppId,
    [Parameter(Mandatory)] [string] $ServicePrincipalId,   # Entra enterprise app object ID
    [Parameter(Mandatory)] [string] $Mailbox,
    [string] $AdminUpn,
    [string] $Name = "m365-carddav"
)
$ErrorActionPreference = "Stop"

Import-Module ExchangeOnlineManagement
$connect = @{ ShowBanner = $false }
if ($AdminUpn) { $connect.UserPrincipalName = $AdminUpn }
Connect-ExchangeOnline @connect

if (-not (Get-Command New-ManagementRoleAssignment -ErrorAction SilentlyContinue)) {
    throw "The signed-in account lacks the required Exchange RBAC role. Re-run with -AdminUpn <exchange admin>."
}

# The scope filters on PrimarySmtpAddress - resolve aliases to the primary address.
$primary = (Get-Mailbox -Identity $Mailbox).PrimarySmtpAddress.ToString()
Write-Host "Mailbox: $primary"

if (-not (Get-ServicePrincipal | Where-Object AppId -eq $AppId)) {
    New-ServicePrincipal -AppId $AppId -ObjectId $ServicePrincipalId -DisplayName $Name | Out-Null
}
if (-not (Get-ManagementScope -Identity "$Name-scope" -ErrorAction SilentlyContinue)) {
    New-ManagementScope -Name "$Name-scope" -RecipientRestrictionFilter "PrimarySmtpAddress -eq '$primary'" | Out-Null
}
if (-not (Get-ManagementRoleAssignment -Identity "$Name-contacts-read" -ErrorAction SilentlyContinue)) {
    New-ManagementRoleAssignment -Name "$Name-contacts-read" -Role "Application Contacts.Read" `
        -App $AppId -CustomResourceScope "$Name-scope" | Out-Null
}

Write-Host ""
Write-Host "Verification (expect 'Application Contacts.Read' with InScope = True):"
Test-ServicePrincipalAuthorization -Identity $AppId -Resource $primary | Format-Table RoleName, AllowedResourceScope, InScope
