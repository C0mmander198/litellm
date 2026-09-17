#!/usr/bin/env bash
set -euo pipefail

config_path="$(pwd -P)/tests/realtime_proxy_smoke/config.yaml"
log_dir="$(mktemp -d)"
proxy_log="${log_dir}/proxy.log"
upstream_log="${log_dir}/upstream.log"
proxy_pid=""
upstream_pid=""

cleanup() {
  if [[ -n "${proxy_pid}" ]]; then
    kill "${proxy_pid}" >/dev/null 2>&1 || true
  fi
  if [[ -n "${upstream_pid}" ]]; then
    kill "${upstream_pid}" >/dev/null 2>&1 || true
  fi
  rm -rf "${log_dir}"
}
trap cleanup EXIT

python3 tests/realtime_proxy_smoke/fake_realtime_upstream.py >"${upstream_log}" 2>&1 &
upstream_pid=$!

LITELLM_MASTER_KEY=sk-test-master \
  SMOKE_PROVIDER_API_BASE=http://127.0.0.1:18765 \
  uv run --no-sync litellm --config "${config_path}" --host 127.0.0.1 --port 14000 \
  >"${proxy_log}" 2>&1 &
proxy_pid=$!

for _ in $(seq 1 90); do
  if curl --fail --silent http://127.0.0.1:14000/health/liveliness >/dev/null; then
    if python3 tests/realtime_proxy_smoke/probe.py; then
      exit 0
    fi
    sed -e 's/sk-test-master/[virtual-key-redacted]/g' \
      -e 's/provider-test-key/[provider-key-redacted]/g' "${proxy_log}"
    exit 1
  fi
  if ! kill -0 "${proxy_pid}" >/dev/null 2>&1; then
    sed -e 's/sk-test-master/[virtual-key-redacted]/g' \
      -e 's/provider-test-key/[provider-key-redacted]/g' "${proxy_log}"
    exit 1
  fi
  sleep 2
done

sed -e 's/sk-test-master/[virtual-key-redacted]/g' \
  -e 's/provider-test-key/[provider-key-redacted]/g' "${proxy_log}"
echo "LiteLLM source integration process did not become live" >&2
exit 1
