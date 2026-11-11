import asyncio
import contextlib
import logging
import os
import random
import sys
import time
import traceback
import uuid

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from prometheus_client import (
    CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, disable_created_metrics, generate_latest,
)
from pydantic import BaseModel, Field

disable_created_metrics()  # hides the noisy *_created series

SERVICE = "inventory-service"

# ---------------------------------------------------------------------------
# Logging: INFO goes to stdout, WARNING and ERROR go to stderr.
# Every line is key=value text, so Loki can parse it with "| logfmt".
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
RESERVATIONS = Counter(
    "inventory_reservations_total", "Stock reservations by result", ["result"]
)
STOCK_LEVEL = Gauge("inventory_stock_units", "Units currently in stock", ["sku"])
CHAOS_RATE = Gauge("demo_chaos_rate", "Current failure injection setting", ["kind"])

# ---------------------------------------------------------------------------
# Demo data and failure-injection settings
# ---------------------------------------------------------------------------
CATALOG = {
    "SKU-1001": {"name": "Mechanical Keyboard", "price": 79.00, "max_stock": 100, "restock": True},
    "SKU-1002": {"name": "USB-C Dock", "price": 129.50, "max_stock": 100, "restock": True},
    "SKU-1003": {"name": "27-inch Monitor", "price": 249.99, "max_stock": 100, "restock": True},
    "SKU-1004": {"name": "Wireless Mouse", "price": 34.90, "max_stock": 100, "restock": True},
    # Limited edition: never restocks by itself, so it sells out under load.
    "SKU-1005": {"name": "Limited Edition Headset", "price": 199.00, "max_stock": 15, "restock": False},
}
stock = {sku: item["max_stock"] for sku, item in CATALOG.items()}

chaos = {
    "error_rate": float(os.getenv("INVENTORY_ERROR_RATE", "0.05")),
    "slow_rate": float(os.getenv("INVENTORY_SLOW_RATE", "0.10")),
}
for kind, value in chaos.items():
    CHAOS_RATE.labels(kind).set(value)
for sku, units in stock.items():
    STOCK_LEVEL.labels(sku).set(units)


async def restock_loop():
    # Adds 20 units every 10 seconds to normal products, up to max_stock.
    while True:
        await asyncio.sleep(10)
        for sku, item in CATALOG.items():
            if item["restock"] and stock[sku] < item["max_stock"]:
                stock[sku] = min(item["max_stock"], stock[sku] + 20)
                STOCK_LEVEL.labels(sku).set(stock[sku])


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    task = asyncio.create_task(restock_loop())
    logger.info("event=startup stock_skus=%d", len(stock))
    yield
    task.cancel()


app = FastAPI(title="Inventory Service", lifespan=lifespan)


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
        logger.info(
            "event=http_request request_id=%s method=%s path=%s status=%d duration_ms=%.1f",
            request_id, request.method, path, response.status_code, duration * 1000,
        )
    return response


class ReserveRequest(BaseModel):
    sku: str
    quantity: int = Field(ge=1, le=100)


class ChaosUpdate(BaseModel):
    error_rate: float | None = Field(default=None, ge=0, le=1)
    slow_rate: float | None = Field(default=None, ge=0, le=1)


@app.get("/health")
def health():
    return {"status": "ok", "service": SERVICE}


@app.get("/metrics")
def metrics():
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.get("/stock")
def list_stock():
    products = []
    for sku, item in CATALOG.items():
        products.append(
            {"sku": sku, "name": item["name"], "price": item["price"], "stock": stock[sku]}
        )
    return {"products": products}


@app.post("/reserve")
async def reserve(body: ReserveRequest, request: Request):
    request_id = request.state.request_id

    # Intentional failure 1: slow response (0.8 - 1.8 seconds).
    if random.random() < chaos["slow_rate"]:
        delay = random.uniform(0.8, 1.8)
        logger.warning(
            "event=slow_stock_lookup request_id=%s sku=%s delay_ms=%.0f",
            request_id, body.sku, delay * 1000,
        )
        await asyncio.sleep(delay)

    # Intentional failure 2: a real unhandled exception (KeyError) with a traceback.
    if random.random() < chaos["error_rate"]:
        RESERVATIONS.labels("error").inc()
        corrupted_row = CATALOG["SKU-9999"]  # does not exist -> KeyError
        logger.info("never reached %s", corrupted_row)

    if body.sku not in CATALOG:
        RESERVATIONS.labels("unknown_sku").inc()
        logger.warning("event=unknown_sku request_id=%s sku=%s", request_id, body.sku)
        return JSONResponse(status_code=404, content={"error": "unknown_sku", "sku": body.sku})

    if stock[body.sku] < body.quantity:
        RESERVATIONS.labels("out_of_stock").inc()
        logger.warning(
            "event=out_of_stock request_id=%s sku=%s requested=%d available=%d",
            request_id, body.sku, body.quantity, stock[body.sku],
        )
        return JSONResponse(
            status_code=409,
            content={"error": "out_of_stock", "sku": body.sku, "available": stock[body.sku]},
        )

    stock[body.sku] -= body.quantity
    STOCK_LEVEL.labels(body.sku).set(stock[body.sku])
    RESERVATIONS.labels("reserved").inc()
    unit_price = CATALOG[body.sku]["price"]
    logger.info(
        "event=stock_reserved request_id=%s sku=%s quantity=%d remaining=%d",
        request_id, body.sku, body.quantity, stock[body.sku],
    )
    return {
        "sku": body.sku,
        "quantity": body.quantity,
        "unit_price": unit_price,
        "total": round(unit_price * body.quantity, 2),
    }


@app.post("/release")
def release(body: ReserveRequest, request: Request):
    if body.sku in CATALOG:
        stock[body.sku] = min(CATALOG[body.sku]["max_stock"], stock[body.sku] + body.quantity)
        STOCK_LEVEL.labels(body.sku).set(stock[body.sku])
        RESERVATIONS.labels("released").inc()
        logger.info(
            "event=stock_released request_id=%s sku=%s quantity=%d remaining=%d",
            request.state.request_id, body.sku, body.quantity, stock[body.sku],
        )
    return {"released": True}


@app.post("/restock")
def restock_all(request: Request):
    for sku, item in CATALOG.items():
        stock[sku] = item["max_stock"]
        STOCK_LEVEL.labels(sku).set(stock[sku])
    logger.info("event=manual_restock request_id=%s", request.state.request_id)
    return {"restocked": True}


@app.get("/chaos")
def get_chaos():
    return chaos


@app.post("/chaos")
def set_chaos(update: ChaosUpdate):
    for kind, value in update.model_dump(exclude_none=True).items():
        chaos[kind] = value
        CHAOS_RATE.labels(kind).set(value)
    logger.warning(
        "event=chaos_updated error_rate=%s slow_rate=%s", chaos["error_rate"], chaos["slow_rate"]
    )
    return chaos
