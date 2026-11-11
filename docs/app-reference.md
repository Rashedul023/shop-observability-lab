# Shop Demo – microservices that fail on purpose

A small online-shop made of **3 FastAPI microservices + a React frontend**.
It exposes real Prometheus metrics, writes real logs to stdout/stderr, and fails in
controlled, repeatable ways, so there is always something interesting to monitor.

```
                         browser
                            |
                  http://127.0.0.1:8080
                            |
                     +------v------+
                     |  frontend   |  React + nginx          (port 80 in container)
                     +------+------+
                            |  /api/*
                     +------v------+  GET  /health
                     |order-service|  POST /api/orders
                     |   :8000     |  GET  /metrics
                     +---+-----+---+
          POST /reserve  |     |  POST /charge
          POST /release  |     |
              +----------v+   +v-----------+
              | inventory |   |  payment   |
              | service   |   |  service   |
              |   :8002   |   |   :8001    |
              +-----------+   +------------+

   traffic-generator (optional) --> order-service     steady / wavy background traffic
```

Every service exposes `GET /health` and `GET /metrics`.
Request IDs travel through all services (`x-request-id`), so one order can be followed in the logs.

---

## 1. Run with Docker Compose

```bash
cp .env.example .env                       # optional, defaults work
docker compose up -d --build               # start the 4 containers
docker compose --profile loadgen up -d     # optional: add background traffic
docker compose ps                          # all should become "healthy"
```

| What                | URL                                  |
|---------------------|--------------------------------------|
| Web UI              | http://127.0.0.1:8080                |
| Order API           | http://127.0.0.1:8000                |
| Order metrics       | http://127.0.0.1:8000/metrics        |
| Payment metrics     | http://127.0.0.1:8001/metrics        |
| Inventory metrics   | http://127.0.0.1:8002/metrics        |

All ports are bound to `127.0.0.1` only. Nothing is reachable from outside your laptop.

```bash
# first request
curl -X POST http://127.0.0.1:8000/api/orders \
  -H 'Content-Type: application/json' \
  -d '{"customer":"mitul","sku":"SKU-1001","quantity":2}'
```

Stop / clean up:

```bash
docker compose --profile loadgen down        # stop and remove containers
docker compose --profile loadgen down --rmi local   # ...and remove built images
```

---

## 2. Where the errors come from

All rates are probabilities (0 – 1). Defaults are in `.env.example` and can be changed
**live** with the sliders in the UI or with `POST /api/chaos`.

| Failure                         | Service   | Default | What you see                                              |
|---------------------------------|-----------|---------|-----------------------------------------------------------|
| Card declined                   | payment   | 10 %    | HTTP 402, WARNING log `card_declined`                     |
| Gateway crash (`ValueError`)    | payment   | 5 %     | payment 500 -> order 502, ERROR log with traceback        |
| Gateway hangs 4 s               | payment   | 3 %     | order waits 2 s -> HTTP 504 `payment_timeout`             |
| Stock lookup crash (`KeyError`) | inventory | 5 %     | inventory 500 -> order 502, ERROR log with traceback      |
| Slow stock lookup (0.8–1.8 s)   | inventory | 10 %    | higher latency, WARNING log `slow_stock_lookup`           |
| Pricing bug (`ZeroDivisionError`)| order    | 3 %     | order 500, ERROR log with traceback                       |
| Out of stock                    | inventory | natural | HTTP 409. SKU-1005 sells out fast; bursts drain the rest  |
| Invalid request body            | order     | on demand | HTTP 422 (quantity 0 or > 10, bad SKU format)           |
| Always-fail endpoint            | order     | on demand | `GET /api/debug/error` -> HTTP 500 every time           |
| Slow endpoint                   | order     | on demand | `GET /api/debug/slow?seconds=4`                         |

When payment fails after stock was reserved, the order-service calls `POST /release`
to give the stock back (a compensating action, visible in the logs).

---

## 3. Metrics reference

Naming follows Prometheus conventions (`_total` for counters, `_seconds` for time).
Label values are always from a small fixed set (route templates, not raw URLs).

| Metric                                  | Type      | Labels                    | Service(s)  |
|-----------------------------------------|-----------|---------------------------|-------------|
| `http_requests_total`                   | counter   | `method, path, status`    | all 3       |
| `http_request_duration_seconds`         | histogram | `method, path`            | all 3       |
| `http_requests_in_progress`             | gauge     | –                         | all 3       |
| `app_unhandled_exceptions_total`        | counter   | `exception`               | all 3       |
| `demo_chaos_rate`                       | gauge     | `kind`                    | all 3       |
| `shop_orders_total`                     | counter   | `outcome`                 | order       |
| `shop_order_revenue_dollars_total`      | counter   | –                         | order       |
| `shop_items_sold_total`                 | counter   | `sku`                     | order       |
| `downstream_requests_total`             | counter   | `target, outcome`         | order       |
| `downstream_request_duration_seconds`   | histogram | `target`                  | order       |
| `payments_total`                        | counter   | `result`                  | payment     |
| `payment_charged_dollars_total`         | counter   | –                         | payment     |
| `inventory_reservations_total`          | counter   | `result`                  | inventory   |
| `inventory_stock_units`                 | gauge     | `sku`                     | inventory   |

`shop_orders_total{outcome=...}`: `created`, `out_of_stock`, `unknown_sku`, `payment_declined`,
`payment_error`, `payment_timeout`, `inventory_error`, `inventory_timeout`, `internal_error`.

Notes:
- `/metrics` itself is not counted. `/health` is counted (Docker/Kubernetes probes call it every few seconds), so filter `path!="/health"` when you want only real traffic.
- Default Python process metrics (`process_*`, `python_*`) are exported too.
- Histogram buckets: 10 ms to 10 s.

---

## 4. Log format

Each line is plain `key=value` text (logfmt), so Loki can parse it with `| logfmt`.

```
ts=2026-11-10T02:21:36+0000 level=INFO  service=order-service event=order_confirmed request_id=453995fb57c3 order_id=ord-d4aa30d1 payment_id=pay-dbc01a46 total=79.00
ts=2026-11-10T02:21:36+0000 level=ERROR service=order-service event=order_failed request_id=9db0237858f0 order_id=ord-653167f9 reason=payment_declined amount=79.00 action=releasing_stock
ts=2026-11-10T02:21:41+0000 level=ERROR service=payment-service event=unhandled_exception request_id=ba1366fd7b16 method=POST path=/charge exception=ValueError error="invalid literal for int() with base 10: 'N/A'"
Traceback (most recent call last):
  File "/app/main.py", line 156, in charge
    gateway_fee = int("N/A")  # gateway returned a non-numeric fee
```

| Stream   | Levels            |
|----------|-------------------|
| stdout   | `INFO`            |
| stderr   | `WARNING`, `ERROR`|

Useful fields: `service`, `level`, `event`, `request_id`, `order_id`, `reason`, `status`, `duration_ms`.
Unhandled exceptions print a short traceback (3 lines, only the failing line of code).
Those continuation lines have no `ts=` prefix, so they show as separate log lines unless you
add a multiline stage in your log collector.

---

## 5. Run on MicroK8s

```bash
microk8s enable dns                     # if not already enabled
./scripts/build-and-load-k8s.sh         # builds images, imports them into MicroK8s, applies manifests
```

What it creates (namespace `shop-demo`):

```
 shop-demo
 |-- deployment/service  frontend            NodePort 30080  -> http://127.0.0.1:30080
 |-- deployment/service  order-service       ClusterIP :8000
 |-- deployment/service  payment-service     ClusterIP :8001
 |-- deployment/service  inventory-service   ClusterIP :8002
 `-- deployment          traffic-generator   1 req/s average, wavy -> constant logs
```

Pods carry `app=<name>` labels and `prometheus.io/*` annotations.

```bash
microk8s kubectl -n shop-demo get pods
microk8s kubectl -n shop-demo logs deploy/order-service --tail=20
microk8s kubectl -n shop-demo logs deploy/payment-service --tail=20
microk8s kubectl -n shop-demo set env deploy/traffic-generator RATE=10      # more traffic
microk8s kubectl delete -f kubernetes/workload.yaml -f kubernetes/namespace.yaml   # clean up
```

Images are imported with `docker save | microk8s ctr image import -`, so no registry is needed
(`imagePullPolicy: IfNotPresent`). After changing code, run the script again, then
`microk8s kubectl -n shop-demo rollout restart deployment`.

---

## 6. Connecting your monitoring stack

- Compose network is named **`shop-net`**. Add your Prometheus (and the rest) to the same
  compose file, or join the network from another project with
  `networks: { shop-net: { external: true } }`.
- Scrape targets inside the network: `order-service:8000`, `payment-service:8001`,
  `inventory-service:8002` (path `/metrics`).
- Docker logs use the `json-file` driver (10 MB x 3 files per container).

---

## 7. Cheat sheet

```
 DEMO GOAL                        COMMAND / ACTION
 -------------------------------  ---------------------------------------------
 normal traffic (low rate)        ./scripts/traffic-test.sh normal
 high traffic (burst)             ./scripts/traffic-test.sh burst
 steady background traffic        docker compose --profile loadgen up -d
 constant 500 errors              ./scripts/traffic-test.sh failure
 high latency                     ./scripts/traffic-test.sh slow
 payment outage (alert test)      ./scripts/traffic-test.sh chaos-on   (+ traffic)
 end the outage                   ./scripts/traffic-test.sh chaos-off
 target DOWN (alert test)         docker compose stop order-service
 target UP again                  docker compose start order-service
 sell-out / 409 errors            order SKU-1005 repeatedly, or burst
 refill stock                     "Restock everything" button / POST /api/restock
 read chaos settings              curl -s http://127.0.0.1:8000/api/chaos
 change a setting                 curl -X POST http://127.0.0.1:8000/api/chaos \
                                    -H 'Content-Type: application/json' \
                                    -d '{"service":"payment","settings":{"decline_rate":0.5}}'
 follow logs (Compose)            docker compose logs -f order-service
 follow logs (MicroK8s)           microk8s kubectl -n shop-demo logs -f deploy/order-service
```

## 8. Project layout

```
.
|-- compose.yaml
|-- .env.example
|-- .gitignore
|-- README.md
|-- app/
|   |-- order-service/      Dockerfile, main.py, requirements.txt
|   |-- payment-service/    Dockerfile, main.py, requirements.txt
|   |-- inventory-service/  Dockerfile, main.py, requirements.txt
|   |-- traffic-generator/  Dockerfile, generator.py, requirements.txt
|   `-- frontend/           Dockerfile (multi-stage), nginx.conf, React + Vite source
|-- kubernetes/
|   |-- namespace.yaml
|   `-- workload.yaml
`-- scripts/
    |-- traffic-test.sh
    `-- build-and-load-k8s.sh
```

Local development without Docker: run each service with
`uvicorn main:app --port <8000|8001|8002>` (set `INVENTORY_URL` and `PAYMENT_URL` for the
order-service) and `npm install && npm run dev` in `app/frontend`.
