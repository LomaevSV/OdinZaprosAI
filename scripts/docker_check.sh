#!/usr/bin/env sh
# Приёмка Docker (T-DLV-DOCKER-01): собирает журналы в docker_check/ и печатает итог PASS/FAIL.
# Запуск из корня репозитория: sh scripts/docker_check.sh
# Ключи не нужны; .env не требуется. Платных запросов нет (режим по умолчанию auto -> mock).
set -u
OUT=docker_check
mkdir -p "$OUT"
fail() { echo "RESULT: FAIL - $1" | tee "$OUT/result.txt"; exit 1; }

{ docker --version; docker compose version; uname -a 2>/dev/null; git rev-parse HEAD; } > "$OUT/env.txt" 2>&1 || fail "docker/compose недоступны (см. $OUT/env.txt)"
cat "$OUT/env.txt"

docker compose up --build -d > "$OUT/docker_up.log" 2>&1 || fail "docker compose up --build (см. $OUT/docker_up.log)"

status=""
for i in $(seq 1 30); do
  status=$(docker inspect --format '{{.State.Health.Status}}' "$(docker compose ps -q app)" 2>/dev/null || echo "none")
  [ "$status" = "healthy" ] && break
  sleep 2
done
docker compose ps > "$OUT/ps.txt" 2>&1
[ "$status" = "healthy" ] || { docker compose logs --no-color app > "$OUT/docker_run.log" 2>&1; fail "контейнер не healthy: $status"; }

docker compose exec -T app python -m odin.diagnose > "$OUT/diagnose.json" 2>&1 || fail "diagnose"
docker compose exec -T app python -m odin.selfcheck > "$OUT/selfcheck.json" 2>&1 || fail "selfcheck (см. $OUT/selfcheck.json)"
curl -fsS http://localhost:8080/health > "$OUT/health.json" 2>&1 || fail "/health с хоста"
code=$(curl -s -o /dev/null -w "%{http_code}" http://localhost:8080/)
[ "$code" = "200" ] || fail "UI / вернул $code"
docker image ls odin-zapros:0.2.0 > "$OUT/image.txt" 2>&1
docker compose logs --no-color app > "$OUT/docker_run.log" 2>&1
docker compose down >> "$OUT/docker_run.log" 2>&1

echo "RESULT: PASS - healthy, diagnose ok, selfcheck PASS, /health и / доступны с хоста" | tee "$OUT/result.txt"
