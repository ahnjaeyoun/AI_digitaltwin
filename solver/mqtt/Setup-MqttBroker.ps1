[CmdletBinding()]
param(
    [string]$BindAddress = '100.116.221.78'
)

$ErrorActionPreference = 'Stop'
$templateFile = Join-Path $PSScriptRoot 'mosquitto-tailscale.conf.template'
$configFile = Join-Path $PSScriptRoot 'mosquitto-tailscale.generated.conf'
$dataDirectory = Join-Path $PSScriptRoot 'data'
$settingsFile = Join-Path (Split-Path $PSScriptRoot -Parent) 'mqtt_settings.json'

New-Item -ItemType Directory -Path $dataDirectory -Force | Out-Null

$config = Get-Content -LiteralPath $templateFile -Raw
$config = $config.Replace('@BIND_ADDRESS@', $BindAddress)
$config = $config.Replace('@DATA_DIRECTORY@', $dataDirectory.Replace('\', '/'))
[IO.File]::WriteAllText($configFile, $config, [Text.UTF8Encoding]::new($false))

if (Test-Path -LiteralPath $settingsFile) {
    $settings = Get-Content -LiteralPath $settingsFile -Raw | ConvertFrom-Json
    $settings.broker.host = $BindAddress
    $settings.publisher.PSObject.Properties.Remove('username')
    $settings.solver.PSObject.Properties.Remove('username')
    $settingsJson = $settings | ConvertTo-Json -Depth 10
    [IO.File]::WriteAllText($settingsFile, $settingsJson, [Text.UTF8Encoding]::new($false))
}

Write-Host "Broker 설정 파일 생성 완료: $configFile"
Write-Host "Tailscale MQTT 접속 주소: $BindAddress`:1883"
Write-Host 'MQTT 계정과 비밀번호를 사용하지 않는 익명 접속이 활성화되었습니다.'
Write-Host '다음 단계: Enable-MqttFirewall.ps1을 관리자로 실행한 후 Start-MqttBroker.ps1을 실행하세요.'
