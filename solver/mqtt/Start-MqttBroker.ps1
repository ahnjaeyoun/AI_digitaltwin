[CmdletBinding()]
param(
    [string]$MosquittoDirectory = 'C:\Program Files\mosquitto'
)

$ErrorActionPreference = 'Stop'
$broker = Join-Path $MosquittoDirectory 'mosquitto.exe'
$config = Join-Path $PSScriptRoot 'mosquitto-tailscale.generated.conf'

if (-not (Test-Path -LiteralPath $broker)) {
    throw "mosquitto.exe를 찾지 못했습니다: $broker"
}
if (-not (Test-Path -LiteralPath $config)) {
    throw "Broker 설정 파일이 없습니다. 먼저 Setup-MqttBroker.ps1을 실행하세요: $config"
}

Write-Host "다음 설정으로 MQTT Broker를 시작합니다: $config"
Write-Host '이 터미널을 열어 두세요. Broker를 중지하려면 Ctrl+C를 누르세요.'
& $broker -c $config -v
exit $LASTEXITCODE
