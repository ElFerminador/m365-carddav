<#
.SYNOPSIS
  Teardown 1 of 2: removes the Exchange Online RBAC objects created by step 2.
  Requires an Exchange administrator (-AdminUpn).
#>
param(
    [Parameter(Mandatory)] [string] $AppId,
    [string] $AdminUpn,
    [string] $Name = "m365-carddav"
)
$ErrorActionPreference = "Stop"

Import-Module ExchangeOnlineManagement
$connect = @{ ShowBanner = $false }
if ($AdminUpn) { $connect.UserPrincipalName = $AdminUpn }
Connect-ExchangeOnline @connect

Get-ManagementRoleAssignment -Identity "$Name-contacts-read" -ErrorAction SilentlyContinue |
    Remove-ManagementRoleAssignment -Confirm:$false
Get-ManagementScope -Identity "$Name-scope" -ErrorAction SilentlyContinue |
    Remove-ManagementScope -Confirm:$false
Get-ServicePrincipal | Where-Object AppId -eq $AppId |
    ForEach-Object { Remove-ServicePrincipal -Identity $_.Identity -Confirm:$false }

Write-Host "Exchange Online objects for '$Name' removed."
