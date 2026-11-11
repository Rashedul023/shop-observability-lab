#!/usr/bin/env bash
# Generates traffic against the order-service from your terminal.
#
#   ./scripts/traffic-test.sh normal      # 30 orders, ~1 per second      (low rate)
#   ./scripts/traffic-test.sh burst       # 300 orders, 25 in parallel    (high rate)
#   ./scripts/traffic-test.sh failure     # 40 requests that always fail  (500 errors)
#   ./scripts/traffic-test.sh slow        # 10 requests that take 4 s     (latency)
#   ./scripts/traffic-test.sh chaos-on    # payment declines/errors 100%  (alert test)
#   ./scripts/traffic-test.sh chaos-off   # back to the default rates
#
# BASE_URL defaults to http://127.0.0.1:8000
set -uo pipefail

BASE_URL="${BASE_URL:-http://127.0.0.1:8000}"
MODE="${1:-normal}"

send_order() {
  sku="SKU-100$((RANDOM % 5 + 1))"
  curl -s -o /dev/null -w "%{http_code}\n" -X POST "$BASE_URL/api/orders" \
    -H "Content-Type: application/json" \
    -d "{\"customer\":\"cli-$RANDOM\",\"sku\":\"$sku\",\"quantity\":$((RANDOM % 3 + 1))}"
}
export -f send_order
export BASE_URL

set_chaos() {
  curl -s -o /dev/null -X POST "$BASE_URL/api/chaos" -H "Content-Type: application/json" -d "$1"
}

echo "mode=$MODE target=$BASE_URL"
case "$MODE" in
  normal)
    for i in $(seq 1 30); do send_order; sleep 1; done | sort | uniq -c
    ;;
  burst)
    seq 1 300 | xargs -P 25 -I{} bash -c 'send_order' | sort | uniq -c
    ;;
  failure)
    for i in $(seq 1 40); do
      curl -s -o /dev/null -w "%{http_code}\n" "$BASE_URL/api/debug/error"; sleep 0.5
    done | sort | uniq -c
    ;;
  slow)
    for i in $(seq 1 10); do
      curl -s -o /dev/null -w "%{http_code} %{time_total}s\n" "$BASE_URL/api/debug/slow?seconds=4"
    done
    ;;
  chaos-on)
    set_chaos '{"service":"payment","settings":{"decline_rate":0.5,"error_rate":0.5,"timeout_rate":0}}'
    echo "payment-service now fails every request. Send traffic, then run: $0 chaos-off"
    ;;
  chaos-off)
    set_chaos '{"service":"payment","settings":{"decline_rate":0.10,"error_rate":0.05,"timeout_rate":0.03}}'
    echo "payment-service back to default failure rates."
    ;;
  *)
    echo "unknown mode: $MODE"; exit 1
    ;;
esac
