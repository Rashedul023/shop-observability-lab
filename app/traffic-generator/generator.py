import asyncio
import math
import os
import random
import sys
import time

import httpx

TARGET_URL = os.getenv("TARGET_URL", "http://order-service:8000")
RATE = float(os.getenv("RATE", "1"))                # average requests per second
WAVE = os.getenv("WAVE", "false").lower() == "true"  # slowly vary the rate up and down
INVALID_PERCENT = float(os.getenv("INVALID_PERCENT", "3"))  # % of requests with a bad body (422)

CUSTOMERS = ["mitul", "mita", "rashed", "sara", "karim", "nadia", "imran", "lina"]
SKUS = ["SKU-1001", "SKU-1002", "SKU-1003", "SKU-1004", "SKU-1005"]
SKU_WEIGHTS = [30, 20, 15, 25, 10]

# Counts of HTTP status codes since the last summary line
status_counts = {}


async def send_one_order(client):
    body = {
        "customer": random.choice(CUSTOMERS),
        "sku": random.choices(SKUS, SKU_WEIGHTS)[0],
        "quantity": random.randint(1, 3),
    }
    if random.random() * 100 < INVALID_PERCENT:
        body["quantity"] = 0  # fails validation -> 422
    try:
        reply = await client.post(f"{TARGET_URL}/api/orders", json=body)
        code = str(reply.status_code)
    except httpx.HTTPError as exc:
        code = type(exc).__name__
    status_counts[code] = status_counts.get(code, 0) + 1


async def main():
    print(f"traffic-generator started target={TARGET_URL} rate={RATE}/s wave={WAVE}", flush=True)
    last_summary = time.time()
    started = time.time()
    async with httpx.AsyncClient(timeout=10) as client:
        while True:
            current_rate = RATE
            if WAVE:
                # Rate swings between 20% and 180% of RATE over a 4-minute cycle
                current_rate = RATE * (1 + 0.8 * math.sin((time.time() - started) / 240 * 2 * math.pi))
            asyncio.create_task(send_one_order(client))
            await asyncio.sleep(random.expovariate(max(current_rate, 0.05)))

            if time.time() - last_summary >= 10:
                print(f"traffic-generator last_10s rate={current_rate:.2f}/s statuses={status_counts}", flush=True)
                status_counts.clear()
                last_summary = time.time()


try:
    asyncio.run(main())
except KeyboardInterrupt:
    sys.exit(0)
