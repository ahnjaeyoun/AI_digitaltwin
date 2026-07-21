[CmdletBinding()]
param(
    [switch]$Full,
    [int]$Limit = 10,
    [double]$Interval = 0
)

$ErrorActionPreference = 'Stop'
$candidates = [System.Collections.Generic.List[string]]::new()
if ($env:PYTHON_EXE) { $candidates.Add($env:PYTHON_EXE) }
Get-ChildItem "$env:LOCALAPPDATA\Programs\Python\Python*\python.exe" -ErrorAction SilentlyContinue |
    Sort-Object FullName -Descending |
    ForEach-Object { $candidates.Add($_.FullName) }
foreach ($name in @('python', 'python3')) {
    $command = Get-Command $name -ErrorAction SilentlyContinue
    if ($command) { $candidates.Add($command.Source) }
}

$python = $null
foreach ($candidate in ($candidates | Select-Object -Unique)) {
    try {
        & $candidate --version *> $null
        if ($LASTEXITCODE -eq 0) {
            $python = $candidate
            break
        }
    } catch {
        continue
    }
}
if (-not $python) {
    throw 'Python 실행 파일을 찾지 못했습니다. PYTHON_EXE 환경 변수를 설정하세요.'
}

$runner = Join-Path $PSScriptRoot 'Run-MqttFromVisualStudio.py'
$arguments = @('-u', $runner)
if ($Full) {
    $arguments += '--full'
} else {
    $arguments += @('--limit', $Limit.ToString(), '--interval', $Interval.ToString([Globalization.CultureInfo]::InvariantCulture))
}

Write-Host "Python 실행 파일: $python"
& $python @arguments
$exitCode = $LASTEXITCODE
Write-Host "MQTT 실행기 종료 코드: $exitCode"
Read-Host '창을 닫으려면 Enter 키를 누르세요'
exit $exitCode
