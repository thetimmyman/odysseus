#!/usr/bin/env bash
set -uo pipefail
cd /home/tdefreest/Documents/PersonalOS/odysseus-github-ps579
export PS632_CAPABILITY_STORE=/home/tdefreest/scratch/ps632-store
CMD=75346adefef9a31cb050b22481957584991f544aba1639f23787c40036f4c525

echo "=== launch $(date -u +%H:%M:%S) ==="
ssh -o BatchMode=yes framework 'set -e
if ss -ltn | grep -q ':8731 '; then echo UNEXPECTED_SERVER_ON_8731; exit 3; fi
docker stop ollama >/dev/null 2>&1 || sudo systemctl stop ollama 2>/dev/null || true
sleep 3
cat /sys/class/drm/card*/device/mem_info_gtt_used 2>/dev/null | head -1
cd /mnt/framework-data/repos/halo-box/llama.cpp/build/bin
nohup setsid ./llama-server -m /mnt/framework-data/models/halogen-flash-same-gguf/UD-IQ4_XS/Qwen3.8-Flash-Next-UD-IQ4_XS-00001-of-00003.gguf -c 262144 -ngl all -b 2048 -ub 512 -fa on --parallel 4 --ngram-on-disk --ngram-io-threads 64 --ngram-cache 0 --metrics --jinja --host 127.0.0.1 --port 8731 > /tmp/halobox-reconcile.log 2>&1 &
echo $! > /tmp/halobox-reconcile.pid
for i in $(seq 1 240); do grep -q "model loaded" /tmp/halobox-reconcile.log 2>/dev/null && curl -fsS http://127.0.0.1:8731/health >/dev/null 2>&1 && { echo HEALTH_OK; break; }; sleep 2; done
curl -sS http://127.0.0.1:8731/health; echo'
echo "launch_rc=$?"

echo "=== heartbeat discover $(date -u +%H:%M:%S) ==="
timeout 900 python3 scripts/odysseus-capability --store "$PS632_CAPABILITY_STORE" discover \
  --target local-framework-halobox --reuse-identity-from-store \
  --configured-context 262144 --safe-context 32768 \
  --engine-demonstrated-context 32768 --semantic-verified-context 19760 \
  --safe-context-source "PS-624 sealed qualification: engine-demonstrated at 32768 (llama-bench depths 0/4K/16K/32K pp+tg, r=5, throughput only, no semantic assertion); semantic context-integrity verified to 19760 tokens (sealed semantic/07-long-context exact marker); no sealed request exercised 65536 or 262144" \
  --backend vulkan --runtime-repository "halo-box/llama.cpp" \
  --runtime-commit 29e091ea5b228ac1735cde369e68e6767a53e510 \
  --host-baseline "{\"gpu\":\"Radeon 8060S / gfx1151\",\"cpu_arch\":\"x86_64\",\"kernel\":\"6.19.10-300.fc44.x86_64\",\"boot_cmdline_digest\":\"$CMD\",\"mesa\":\"RADV STRIX_HALO\"}" \
  --notes "PS-632 reconciliation 2026-09-16: executed server identity -ngl all (sealed launch.sh); declared_context now the native 262144 window; safe bound is engine-demonstrated, distinct from semantic verification" \
  | tee /tmp/halobox-reconcile-discover.json | python3 -c "
import sys,json
raw=sys.stdin.read(); start=raw.find('{')
d=json.loads(raw[start:])
print('receipt_hash :', d['receipt_hash'])
print('profile_id   :', d['profile_id'])
print('supersedes?  : previous_identity_drift=', d.get('identity_drift'))
print('context      :', json.dumps(d['context']))
print('declared_ctx :', d['model'].get('declared_context'))
print('served_ctx   :', d['context']['served_context'])
print('measured caps:', d['measured_capabilities'])
print('health       :', d['health'], d['health_state'])"
echo "discover_rc=$?"

echo "=== HALOBOX_ADAPTER_SMOKE_TEST $(date -u +%H:%M:%S) ==="
timeout 900 python3 scripts/ps579-halobox-smoke.py 2>&1 | tail -8
echo "smoke_rc=$?"

echo "=== teardown $(date -u +%H:%M:%S) ==="
ssh -o BatchMode=yes framework 'set -e
fuser -k 8731/tcp 2>/dev/null || true
sleep 6
if ss -ltn | grep -q ':8731 '; then echo STILL_LISTENING_8731; else echo PORT_8731_FREE; fi
cat /sys/class/drm/card*/device/mem_info_gtt_used 2>/dev/null | head -1
docker start ollama >/dev/null 2>&1 || docker restart ollama >/dev/null 2>&1
for i in $(seq 1 40); do curl -fsS http://127.0.0.1:11434/api/ps >/dev/null 2>&1 && break; sleep 2; done
curl -sS --max-time 20 http://127.0.0.1:11434/api/ps | head -c 300; echo
sudo /usr/local/bin/framework-ai-health-check.sh 2>&1 | tail -3'
echo "teardown_rc=$?"
