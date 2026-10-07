<#
.SYNOPSIS
  Teardown 2 of 2: deletes the Entra app registration (and its service principal).
#>
param(
    [Parameter(Mandatory)] [string] $AppId
)
$ErrorActionPreference = "Stop"

Import-Module Microsoft.Graph.Applications
Connect-MgGraph -Scopes "Application.ReadWrite.All" -NoWelcome

$app = Get-MgApplication -Filter "appId eq '$AppId'"
if (-not $app) { throw "No app registration with AppId $AppId found." }
Remove-MgApplication -ApplicationId $app.Id   # also removes the service principal
Write-Host "App registration '$($app.DisplayName)' ($AppId) removed."
