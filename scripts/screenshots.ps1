# Captures d'écran de l'interface pour le rapport (Chrome headless).
# Usage : powershell -File scripts/screenshots.ps1 [-Base http://localhost:8000]
param([string]$Base = "http://localhost:8000")

$chrome = "C:\Program Files\Google\Chrome\Application\chrome.exe"
if (-not (Test-Path $chrome)) { $chrome = "C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe" }
$out = Join-Path $PSScriptRoot "..\docs\rapport\img"
New-Item -ItemType Directory -Force $out | Out-Null

$shots = @(
  @{ name = "tp2_recherche.png"; url = "$Base/?tab=search&q=bail%20rural%20cong%C3%A9%20reprise&case=23-20.161&summary=1"; h = 1180 },
  @{ name = "tp3_agent.png";     url = "$Base/?trace=scenario_s2";  h = 1250 },
  @{ name = "tp3_agent_s3.png";  url = "$Base/?trace=scenario_s3";  h = 1500 }
)
foreach ($s in $shots) {
  $file = Join-Path $out $s.name
  & $chrome --headless=new --disable-gpu --hide-scrollbars --force-device-scale-factor=1.5 `
    "--window-size=1440,$($s.h)" --virtual-time-budget=20000 "--screenshot=$file" $s.url 2>$null | Out-Null
  Write-Output "$($s.name) -> $file"
}
