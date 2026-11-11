import asyncio
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

SERVICE = "payment-service"

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
# Prometheus metrics
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
PAYMENTS = Counter("payments_total", "Payment attempts by result", ["result"])
PAYMENT_DOLLARS = Counter("payment_charged_dollars_total", "Dollars successfully charged")
CHAOS_RATE = Gauge("demo_chaos_rate", "Current failure injection setting", ["kind"])

# ---------------------------------------------------------------------------
# Failure-injection settings (probabilities between 0 and 1)
# ---------------------------------------------------------------------------
chaos = {
    "decline_rate": float(os.getenv("PAYMENT_DECLINE_RATE", "0.10")),
    "error_rate": float(os.getenv("PAYMENT_ERROR_RATE", "0.05")),
    "timeout_rate": float(os.getenv("PAYMENT_TIMEOUT_RATE", "0.03")),
}
for kind, value in chaos.items():
    CHAOS_RATE.labels(kind).set(value)

app = FastAPI(title="Payment Service")


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


class ChargeRequest(BaseModel):
    order_id: str
    customer: str
    amount: float = Field(gt=0)


class ChaosUpdate(BaseModel):
    decline_rate: float | None = Field(default=None, ge=0, le=1)
    error_rate: float | None = Field(default=None, ge=0, le=1)
    timeout_rate: float | None = Field(default=None, ge=0, le=1)


@app.get("/health")
def health():
    return {"status": "ok", "service": SERVICE}


@app.get("/metrics")
def metrics():
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.post("/charge")
async def charge(body: ChargeRequest, request: Request):
    request_id = request.state.request_id
    logger.info(
        "event=charge_started request_id=%s order_id=%s amount=%.2f",
        request_id, body.order_id, body.amount,
    )

    # One random roll decides which (if any) failure is injected.
    roll = random.random()
    timeout_limit = chaos["timeout_rate"]
    error_limit = timeout_limit + chaos["error_rate"]
    decline_limit = error_limit + chaos["decline_rate"]

    if roll < timeout_limit:
        # Intentional failure 1: the payment gateway hangs longer than the caller waits.
        PAYMENTS.labels("timeout").inc()
        logger.warning(
            "event=gateway_slow request_id=%s order_id=%s waiting_seconds=4",
            request_id, body.order_id,
        )
        await asyncio.sleep(4)

    elif roll < error_limit:
        # Intentional failure 2: unhandled exception (ValueError) with a traceback.
        PAYMENTS.labels("error").inc()
        gateway_fee = int("N/A")  # gateway returned a non-numeric fee
        logger.info("never reached %s", gateway_fee)

    elif roll < decline_limit:
        # Intentional failure 3: normal business failure, card declined.
        PAYMENTS.labels("declined").inc()
        logger.warning(
            "event=card_declined request_id=%s order_id=%s customer=%s amount=%.2f",
            request_id, body.order_id, body.customer, body.amount,
        )
        return JSONResponse(
            status_code=402,
            content={"error": "card_declined", "order_id": body.order_id},
        )

    await asyncio.sleep(random.uniform(0.02, 0.15))  # normal processing time
    payment_id = "pay-" + uuid.uuid4().hex[:8]
    PAYMENTS.labels("approved").inc()
    PAYMENT_DOLLARS.inc(body.amount)
    logger.info(
        "event=payment_approved request_id=%s order_id=%s payment_id=%s amount=%.2f",
        request_id, body.order_id, payment_id, body.amount,
    )
    return {"payment_id": payment_id, "status": "approved", "amount": body.amount}


@app.get("/chaos")
def get_chaos():
    return chaos


@app.post("/chaos")
def set_chaos(update: ChaosUpdate):
    for kind, value in update.model_dump(exclude_none=True).items():
        chaos[kind] = value
        CHAOS_RATE.labels(kind).set(value)
    logger.warning(
        "event=chaos_updated decline_rate=%s error_rate=%s timeout_rate=%s",
        chaos["decline_rate"], chaos["error_rate"], chaos["timeout_rate"],
    )
    return chaos
