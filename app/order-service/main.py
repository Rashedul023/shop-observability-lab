import asyncio
import collections
import logging
import os
import random
import sys
import time
import traceback
import uuid

import httpx
from fastapi import FastAPI, Query, Request
from fastapi.responses import JSONResponse, Response
from prometheus_client import (
    CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, disable_created_metrics, generate_latest,
)
from pydantic import BaseModel, Field

disable_created_metrics()  # hides the noisy *_created series

SERVICE = "order-service"
INVENTORY_URL = os.getenv("INVENTORY_URL", "http://inventory-service:8002")
PAYMENT_URL = os.getenv("PAYMENT_URL", "http://payment-service:8001")
DOWNSTREAM_TIMEOUT = float(os.getenv("DOWNSTREAM_TIMEOUT_SECONDS", "2"))

# ---------------------------------------------------------------------------
# Logging: INFO goes to stdout, WARNING and ERROR go to stderr.
# ---------------------------------------------------------------------------
logger = logging.getLogger(SERVICE)
logger.setLevel(logging.INFO)
logger.propagate = False
log_format = logging.Formatter(
    "ts=%(asctime)s level=%(levelname)s service=" + SERVICE + " %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S%z",
)
stdout_handler = logging.StreamHandler(sys.stdout)
stdout_handler.setLevel(logging.INFO)
stdout_handler.addFilter(lambda record: record.levelno < logging.WARNING)
stdout_handler.setFormatter(log_format)
stderr_handler = logging.StreamHandler(sys.stderr)
stderr_handler.setLevel(logging.WARNING)
stderr_handler.setFormatter(log_format)
logger.addHandler(stdout_handler)
logger.addHandler(stderr_handler)

# ---------------------------------------------------------------------------
# Prometheus metrics (all labels have a small, fixed set of values)
# ---------------------------------------------------------------------------
HTTP_REQUESTS = Counter(
    "http_requests_total", "Total HTTP requests", ["method", "path", "status"]
)
HTTP_LATENCY = Histogram(
    "http_request_duration_seconds",
    "HTTP request latency in seconds",
    ["method", "path"],
    buckets=(0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10),
)
HTTP_IN_PROGRESS = Gauge("http_requests_in_progress", "HTTP requests currently being handled")
UNHANDLED_EXCEPTIONS = Counter(
    "app_unhandled_exceptions_total", "Unhandled exceptions", ["exception"]
)
# Business-event metrics
ORDERS = Counter("shop_orders_total", "Orders by final outcome", ["outcome"])
ORDER_REVENUE = Counter("shop_order_revenue_dollars_total", "Dollars from confirmed orders")
ITEMS_SOLD = Counter("shop_items_sold_total", "Units sold by SKU", ["sku"])
# Calls to other services
DOWNSTREAM_REQUESTS = Counter(
    "downstream_requests_total", "Calls to other services", ["target", "outcome"]
)
DOWNSTREAM_LATENCY = Histogram(
    "downstream_request_duration_seconds",
    "Latency of calls to other services in seconds",
    ["target"],
    buckets=(0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2, 2.5, 5),
)
CHAOS_RATE = Gauge("demo_chaos_rate", "Current failure injection setting", ["kind"])

# ---------------------------------------------------------------------------
# Failure-injection settings and in-memory state
# ---------------------------------------------------------------------------
chaos = {"error_rate": float(os.getenv("ORDER_ERROR_RATE", "0.03"))}
CHAOS_RATE.labels("error_rate").set(chaos["error_rate"])
recent_orders = collections.deque(maxlen=20)

http = httpx.AsyncClient(timeout=DOWNSTREAM_TIMEOUT)

app = FastAPI(title="Order Service")


@app.middleware("http")
async def observe_requests(request: Request, call_next):
    request_id = request.headers.get("x-request-id") or uuid.uuid4().hex[:12]
    request.state.request_id = request_id
    start = time.perf_counter()
    HTTP_IN_PROGRESS.inc()
    try:
        response = await call_next(request)
    except Exception as exc:
        UNHANDLED_EXCEPTIONS.labels(type(exc).__name__).inc()
        # One searchable line, then a short traceback showing only the failing line of code.
        logger.error(
            'event=unhandled_exception request_id=%s method=%s path=%s exception=%s error="%s"\n%s',
            request_id, request.method, request.url.path, type(exc).__name__, exc,
            traceback.format_exc(limit=-1).strip(),
        )
        response = JSONResponse(
            status_code=500,
            content={"error": "internal_error", "service": SERVICE, "request_id": request_id},
        )
    finally:
        HTTP_IN_PROGRESS.dec()

    duration = time.perf_counter() - start
    route = request.scope.get("route")
    path = route.path if route else "unmatched"
    if path != "/metrics":
        HTTP_REQUESTS.labels(request.method, path, str(response.status_code)).inc()
        HTTP_LATENCY.labels(request.method, path).observe(duration)
    if path not in ("/metrics", "/health"):
        level = logging.WARNING if response.status_code >= 400 else logging.INFO
        logger.log(
            level,
            "event=http_request request_id=%s method=%s path=%s status=%d duration_ms=%.1f",
            request_id, request.method, path, response.status_code, duration * 1000,
        )
    response.headers["x-request-id"] = request_id
    return response


class OrderRequest(BaseModel):
    customer: str = Field(min_length=1, max_length=50)
    sku: str = Field(pattern=r"^SKU-\d{4}$")
    quantity: int = Field(ge=1, le=10)


class ChaosForward(BaseModel):
    service: str = Field(pattern="^(order|payment|inventory)$")
    settings: dict[str, float]


@app.get("/health")
def health():
    return {"status": "ok", "service": SERVICE}


@app.get("/metrics")
def metrics():
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.get("/api/products")
async def list_products(request: Request):
    try:
        reply = await http.get(f"{INVENTORY_URL}/stock")
        return reply.json()
    except httpx.HTTPError as exc:
        logger.error(
            "event=products_unavailable request_id=%s error=%s",
            request.state.request_id, type(exc).__name__,
        )
        return JSONResponse(status_code=503, content={"error": "inventory_unavailable"})


@app.get("/api/orders")
def list_orders():
    return {"orders": list(recent_orders)}


@app.post("/api/orders")
async def create_order(order: OrderRequest, request: Request):
    request_id = request.state.request_id
    order_id = "ord-" + uuid.uuid4().hex[:8]
    headers = {"x-request-id": request_id}
    logger.info(
        "event=order_received request_id=%s order_id=%s customer=%s sku=%s quantity=%d",
        request_id, order_id, order.customer, order.sku, order.quantity,
    )

    # Intentional failure: a "pricing bug" that crashes some orders with ZeroDivisionError.
    if random.random() < chaos["error_rate"]:
        ORDERS.labels("internal_error").inc()
        loyalty_points = 100 // (order.quantity - order.quantity)
        logger.info("never reached %s", loyalty_points)

    # Step 1: reserve stock in the inventory service
    started = time.perf_counter()
    try:
        reserve_reply = await http.post(
            f"{INVENTORY_URL}/reserve",
            json={"sku": order.sku, "quantity": order.quantity},
            headers=headers,
        )
    except httpx.TimeoutException:
        DOWNSTREAM_REQUESTS.labels("inventory", "timeout").inc()
        DOWNSTREAM_LATENCY.labels("inventory").observe(time.perf_counter() - started)
        ORDERS.labels("inventory_timeout").inc()
        logger.error(
            "event=order_failed request_id=%s order_id=%s reason=inventory_timeout timeout_seconds=%s",
            request_id, order_id, DOWNSTREAM_TIMEOUT,
        )
        return JSONResponse(
            status_code=504,
            content={"error": "inventory_timeout", "order_id": order_id, "request_id": request_id},
        )
    except httpx.HTTPError as exc:
        DOWNSTREAM_REQUESTS.labels("inventory", "unreachable").inc()
        ORDERS.labels("inventory_error").inc()
        logger.error(
            "event=order_failed request_id=%s order_id=%s reason=inventory_unreachable error=%s",
            request_id, order_id, type(exc).__name__,
        )
        return JSONResponse(
            status_code=503,
            content={"error": "inventory_unavailable", "order_id": order_id, "request_id": request_id},
        )
    DOWNSTREAM_LATENCY.labels("inventory").observe(time.perf_counter() - started)

    if reserve_reply.status_code == 404:
        DOWNSTREAM_REQUESTS.labels("inventory", "client_error").inc()
        ORDERS.labels("unknown_sku").inc()
        logger.warning(
            "event=order_rejected request_id=%s order_id=%s reason=unknown_sku sku=%s",
            request_id, order_id, order.sku,
        )
        return JSONResponse(
            status_code=404,
            content={"error": "unknown_sku", "order_id": order_id, "request_id": request_id},
        )
    if reserve_reply.status_code == 409:
        DOWNSTREAM_REQUESTS.labels("inventory", "client_error").inc()
        ORDERS.labels("out_of_stock").inc()
        logger.warning(
            "event=order_rejected request_id=%s order_id=%s reason=out_of_stock sku=%s",
            request_id, order_id, order.sku,
        )
        return JSONResponse(
            status_code=409,
            content={"error": "out_of_stock", "order_id": order_id, "request_id": request_id},
        )
    if reserve_reply.status_code >= 500:
        DOWNSTREAM_REQUESTS.labels("inventory", "server_error").inc()
        ORDERS.labels("inventory_error").inc()
        logger.error(
            "event=order_failed request_id=%s order_id=%s reason=inventory_error downstream_status=%d",
            request_id, order_id, reserve_reply.status_code,
        )
        return JSONResponse(
            status_code=502,
            content={"error": "inventory_error", "order_id": order_id, "request_id": request_id},
        )
    DOWNSTREAM_REQUESTS.labels("inventory", "ok").inc()
    reservation = reserve_reply.json()
    total = reservation["total"]

    # Step 2: charge the customer in the payment service
    started = time.perf_counter()
    payment_failure = None
    try:
        pay_reply = await http.post(
            f"{PAYMENT_URL}/charge",
            json={"order_id": order_id, "customer": order.customer, "amount": total},
            headers=headers,
        )
        DOWNSTREAM_LATENCY.labels("payment").observe(time.perf_counter() - started)
        if pay_reply.status_code == 402:
            DOWNSTREAM_REQUESTS.labels("payment", "client_error").inc()
            payment_failure = ("payment_declined", 402)
        elif pay_reply.status_code >= 500:
            DOWNSTREAM_REQUESTS.labels("payment", "server_error").inc()
            payment_failure = ("payment_error", 502)
        else:
            DOWNSTREAM_REQUESTS.labels("payment", "ok").inc()
    except httpx.TimeoutException:
        DOWNSTREAM_REQUESTS.labels("payment", "timeout").inc()
        DOWNSTREAM_LATENCY.labels("payment").observe(time.perf_counter() - started)
        payment_failure = ("payment_timeout", 504)
    except httpx.HTTPError:
        DOWNSTREAM_REQUESTS.labels("payment", "unreachable").inc()
        payment_failure = ("payment_error", 503)

    if payment_failure is not None:
        reason, status_code = payment_failure
        ORDERS.labels(reason).inc()
        logger.error(
            "event=order_failed request_id=%s order_id=%s reason=%s amount=%.2f action=releasing_stock",
            request_id, order_id, reason, total,
        )
        # Compensating action: give the reserved stock back (best effort).
        try:
            await http.post(
                f"{INVENTORY_URL}/release",
                json={"sku": order.sku, "quantity": order.quantity},
                headers=headers,
            )
        except httpx.HTTPError:
            logger.error(
                "event=release_failed request_id=%s order_id=%s sku=%s",
                request_id, order_id, order.sku,
            )
        return JSONResponse(
            status_code=status_code,
            content={"error": reason, "order_id": order_id, "request_id": request_id},
        )

    # Step 3: success
    payment = pay_reply.json()
    ORDERS.labels("created").inc()
    ORDER_REVENUE.inc(total)
    ITEMS_SOLD.labels(order.sku).inc(order.quantity)
    result = {
        "order_id": order_id,
        "status": "confirmed",
        "customer": order.customer,
        "sku": order.sku,
        "quantity": order.quantity,
        "total": total,
        "payment_id": payment["payment_id"],
    }
    recent_orders.appendleft(result)
    logger.info(
        "event=order_confirmed request_id=%s order_id=%s payment_id=%s total=%.2f",
        request_id, order_id, payment["payment_id"], total,
    )
    return JSONResponse(status_code=201, content=result)


@app.post("/api/restock")
async def restock(request: Request):
    reply = await http.post(f"{INVENTORY_URL}/restock", headers={"x-request-id": request.state.request_id})
    return reply.json()


@app.get("/api/chaos")
async def get_chaos():
    settings = {"order": chaos}
    try:
        settings["payment"] = (await http.get(f"{PAYMENT_URL}/chaos")).json()
        settings["inventory"] = (await http.get(f"{INVENTORY_URL}/chaos")).json()
    except httpx.HTTPError:
        logger.error("event=chaos_read_failed")
    return settings


@app.post("/api/chaos")
async def set_chaos(body: ChaosForward):
    if body.service == "order":
        if "error_rate" in body.settings:
            chaos["error_rate"] = min(1.0, max(0.0, body.settings["error_rate"]))
            CHAOS_RATE.labels("error_rate").set(chaos["error_rate"])
        logger.warning("event=chaos_updated error_rate=%s", chaos["error_rate"])
        return {"order": chaos}
    target = PAYMENT_URL if body.service == "payment" else INVENTORY_URL
    reply = await http.post(f"{target}/chaos", json=body.settings)
    return JSONResponse(status_code=reply.status_code, content={body.service: reply.json()})


# ---------------------------------------------------------------------------
# Deterministic failures for demos and alert tests
# ---------------------------------------------------------------------------
@app.get("/api/debug/error")
def debug_error():
    # Always fails with a 500 and a traceback on stderr.
    raise RuntimeError("intentional demo error from /api/debug/error")


@app.get("/api/debug/slow")
async def debug_slow(seconds: float = Query(default=3, ge=0, le=15)):
    logger.warning("event=debug_slow_request seconds=%s", seconds)
    await asyncio.sleep(seconds)
    return {"slept_seconds": seconds}
