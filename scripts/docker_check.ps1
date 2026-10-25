# Docker acceptance check (T-DLV-DOCKER-01) for Windows PowerShell. ASCII-only on purpose:
# PowerShell 5.1 reads BOM-less scripts as ANSI. Logs go to docker_check\, final line is PASS/FAIL.
# Run from the repository root:  powershell -ExecutionPolicy Bypass -File scripts\docker_check.ps1
# No keys and no .env needed; no paid calls (default data mode auto -> mock).
$ErrorActionPreference = "Continue"
$out = "docker_check"
New-Item -ItemType Directory -Force -Path $out | Out-Null
function Fail($msg) { "RESULT: FAIL - $msg" | Tee-Object -FilePath "$out\result.txt"; exit 1 }

& { docker --version; docker compose version; [Environment]::OSVersion.VersionString; git rev-parse HEAD } *> "$out\env.txt"
if ($LASTEXITCODE -ne 0) { Fail "docker/compose not available (see $out\env.txt)" }
Get-Content "$out\env.txt"

docker compose up --build -d *> "$out\docker_up.log"
if ($LASTEXITCODE -ne 0) { Fail "docker compose up --build (see $out\docker_up.log)" }

$status = "none"
for ($i = 0; $i -lt 30; $i++) {
  $id = docker compose ps -q app
  $status = docker inspect --format "{{.State.Health.Status}}" $id 2>$null
  if ($status -eq "healthy") { break }
  Start-Sleep -Seconds 2
}
docker compose ps *> "$out\ps.txt"
if ($status -ne "healthy") { docker compose logs --no-color app *> "$out\docker_run.log"; Fail "container is not healthy: $status" }

docker compose exec -T app python -m odin.diagnose *> "$out\diagnose.json"
if ($LASTEXITCODE -ne 0) { Fail "diagnose" }
docker compose exec -T app python -m odin.selfcheck *> "$out\selfcheck.json"
if ($LASTEXITCODE -ne 0) { Fail "selfcheck (see $out\selfcheck.json)" }
try {
  (Invoke-WebRequest -UseBasicParsing http://localhost:8080/health -TimeoutSec 10).Content | Set-Content "$out\health.json"
  $code = (Invoke-WebRequest -UseBasicParsing http://localhost:8080/ -TimeoutSec 10).StatusCode
} catch { Fail "HTTP from host: $($_.Exception.Message)" }
if ($code -ne 200) { Fail "UI / returned $code" }
docker image ls odin-zapros:0.2.0 *> "$out\image.txt"
docker compose logs --no-color app *> "$out\docker_run.log"
docker compose down *>> "$out\docker_run.log"

"RESULT: PASS - healthy, diagnose ok, selfcheck PASS, /health and / reachable from host" | Tee-Object -FilePath "$out\result.txt"
