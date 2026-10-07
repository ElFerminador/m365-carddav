<#
.SYNOPSIS
  Step 2 of 2: grants the app read access to contacts - for one mailbox or for
  all members of a group.

.DESCRIPTION
  Uses Exchange Online "RBAC for Applications":
    - registers the service principal in Exchange Online
    - creates (or updates) a management scope that matches either
        -Mailbox  exactly one mailbox, or
        -Group    all DIRECT members of a group (Microsoft 365 group,
                  mail-enabled security group or distribution list;
                  nested groups are NOT evaluated)
    - assigns the role "Application Contacts.Read" limited to that scope

  With -Group, adding/removing a user later is just a group membership change;
  the app and this script stay untouched.

  Re-running the script is safe. Running it with -Group on an existing
  single-mailbox setup switches the scope to the group.

  Required role: Exchange Administrator / Organization Management.
  A normal user account does not even see the cmdlets used here
  (New-ServicePrincipal "is not recognized"). Pass -AdminUpn to sign in
  with the admin account.

.EXAMPLE
  ./2-Grant-MailboxAccess.ps1 -AppId <guid> -ServicePrincipalId <guid> `
      -Mailbox user@contoso.com -AdminUpn admin@contoso.onmicrosoft.com

.EXAMPLE
  ./2-Grant-MailboxAccess.ps1 -AppId <guid> -ServicePrincipalId <guid> `
      -Group carddav-users@contoso.com -AdminUpn admin@contoso.onmicrosoft.com
#>
[CmdletBinding(DefaultParameterSetName = "Mailbox")]
param(
    [Parameter(Mandatory)] [string] $AppId,
    [Parameter(Mandatory)] [string] $ServicePrincipalId,   # Entra enterprise app object ID
    [Parameter(Mandatory, ParameterSetName = "Mailbox")] [string] $Mailbox,
    [Parameter(Mandatory, ParameterSetName = "Group")]   [string] $Group,
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

if ($PSCmdlet.ParameterSetName -eq "Mailbox") {
    # The scope filters on PrimarySmtpAddress - resolve aliases to the primary address.
    $primary = (Get-Mailbox -Identity $Mailbox).PrimarySmtpAddress.ToString()
    $filter  = "PrimarySmtpAddress -eq '$primary'"
    $testTargets = @($primary)
    Write-Host "Scope: mailbox $primary"
} else {
    $groups = @(Get-Group -Identity $Group -ErrorAction Stop)
    if ($groups.Count -ne 1) {
        $list = ($groups | ForEach-Object { "  $($_.Name) [$($_.RecipientTypeDetails)] $($_.DistinguishedName)" }) -join "`n"
        throw "'$Group' matches $($groups.Count) groups - use the group's e-mail address instead:`n$list"
    }
    $supported = "MailUniversalSecurityGroup", "MailUniversalDistributionGroup", "GroupMailbox"
    if ($groups[0].RecipientTypeDetails -notin $supported) {
        throw "'$Group' is a $($groups[0].RecipientTypeDetails). RBAC for Applications only evaluates " +
              "mail-enabled security groups, distribution lists and Microsoft 365 groups."
    }
    $dn = $groups[0].DistinguishedName
    $filter = "MemberOfGroup -eq '$dn'"
    $testTargets = @(Get-Recipient -RecipientTypeDetails UserMailbox -Filter $filter -ResultSize 20 |
                     ForEach-Object { $_.PrimarySmtpAddress.ToString() })
    Write-Host "Scope: direct members of $Group ($($testTargets.Count) mailbox(es) found, max. 20 shown)"
}

if (-not (Get-ServicePrincipal | Where-Object AppId -eq $AppId)) {
    New-ServicePrincipal -AppId $AppId -ObjectId $ServicePrincipalId -DisplayName $Name | Out-Null
}

$scope = Get-ManagementScope -Identity "$Name-scope" -ErrorAction SilentlyContinue
if (-not $scope) {
    New-ManagementScope -Name "$Name-scope" -RecipientRestrictionFilter $filter | Out-Null
} elseif ($scope.RecipientFilter -ne $filter) {
    Write-Host "Updating scope filter: '$($scope.RecipientFilter)' -> '$filter'"
    Set-ManagementScope -Identity "$Name-scope" -RecipientRestrictionFilter $filter
}

if (-not (Get-ManagementRoleAssignment -Identity "$Name-contacts-read" -ErrorAction SilentlyContinue)) {
    New-ManagementRoleAssignment -Name "$Name-contacts-read" -Role "Application Contacts.Read" `
        -App $AppId -CustomResourceScope "$Name-scope" | Out-Null
}

Write-Host ""
Write-Host "Verification (expect 'Application Contacts.Read' with InScope = True;"
Write-Host "changes can take up to 2 hours to become effective for Graph):"
foreach ($t in $testTargets) {
    $r = Test-ServicePrincipalAuthorization -Identity $AppId -Resource $t |
         Where-Object RoleName -eq "Application Contacts.Read"
    "{0,-45} InScope = {1}" -f $t, ($r.InScope -join ",")
}
