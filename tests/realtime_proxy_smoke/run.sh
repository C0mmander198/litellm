#!/usr/bin/env bash
set -euo pipefail

image="${1:?image tag required}"
container_name="litellm-realtime-smoke-${GITHUB_RUN_ID:-local}"
config_path="$(pwd -P)/tests/realtime_proxy_smoke/config.yaml"
upstream_pid=""

if [[ ! -f "${config_path}" ]]; then
  echo "Smoke config is not a regular file: ${config_path}" >&2
  exit 1
fi

cleanup() {
  docker rm -f "${container_name}" >/dev/null 2>&1 || true
  if [[ -n "${upstream_pid}" ]]; then
    kill "${upstream_pid}" >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT

python3 tests/realtime_proxy_smoke/fake_realtime_upstream.py &
upstream_pid=$!

docker run --detach --name "${container_name}" \
  --add-host host.docker.internal:host-gateway \
  --publish 14000:4000 \
  --env LITELLM_MASTER_KEY=sk-test-master \
  --env SMOKE_PROVIDER_API_KEY=provider-test-key \
  --mount "type=bind,source=${config_path},target=/app/realtime-smoke-config.yaml,readonly" \
  "${image}" --config /app/realtime-smoke-config.yaml --port 4000 >/dev/null

for _ in $(seq 1 90); do
  if curl --fail --silent http://127.0.0.1:14000/health/liveliness >/dev/null; then
    if python3 tests/realtime_proxy_smoke/probe.py; then
      exit 0
    fi
    docker logs "${container_name}" 2>&1 \
      | sed -e 's/sk-test-master/[virtual-key-redacted]/g' -e 's/provider-test-key/[provider-key-redacted]/g'
    exit 1
  fi
  if ! docker inspect --format '{{.State.Running}}' "${container_name}" | grep -q true; then
    docker logs "${container_name}"
    exit 1
  fi
  sleep 2
done

docker logs "${container_name}"
echo "LiteLLM smoke container did not become live" >&2
exit 1
