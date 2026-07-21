#Requires -RunAsAdministrator
[CmdletBinding()]
param(
    [string]$BrokerAddress = '100.116.221.78',
    [string]$RemoteSubnet = '100.64.0.0/10',
    [int]$Port = 1883
)

$ErrorActionPreference = 'Stop'
$ruleName = 'Hydraulic MQTT Broker Tailscale'
$existing = Get-NetFirewallRule -DisplayName $ruleName -ErrorAction SilentlyContinue
if ($existing) {
    Write-Host "방화벽 규칙이 이미 있습니다: $ruleName"
    exit 0
}

New-NetFirewallRule `
    -DisplayName $ruleName `
    -Direction Inbound `
    -Action Allow `
    -Protocol TCP `
    -LocalAddress $BrokerAddress `
    -LocalPort $Port `
    -RemoteAddress $RemoteSubnet `
    -Profile Any | Out-Null

Write-Host "Tailscale 범위 $RemoteSubnet에서 TCP $BrokerAddress`:$Port 접속을 허용했습니다."
