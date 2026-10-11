"""User-flow prober: a few virtual shoppers hitting the frontend, measuring what users actually feel.

Online Boutique exposes no app metrics, so this probe is the source of truth for error rate and latency.
Each virtual user keeps its own session cookie and cycles home -> product -> add to cart -> cart -> checkout.
"""
from __future__ import annotations

import asyncio
import math
import random
import time
from dataclasses import dataclass

import httpx

from . import config as cfg
from .models import FlowStats

FLOWS = ["home", "product", "add_to_cart", "view_cart", "checkout"]

# Product IDs from Online Boutique's catalog (products.json in productcatalogservice).
PRODUCT_IDS = ["OLJCESPC7Z", "66VCHSJNUP", "1YMWWN1N4O", "L9ECAV7KIM", "2ZYFJ3GM2N",
               "0PUK6V6EV0", "LS4PSXUNUM", "9SIQT8TOJO", "6E92ZMYYFZ"]

# Test checkout data (a Luhn-valid test Visa; the frontend rejects card numbers with dashes).
CHECKOUT_FORM = {
    "email": "someone@example.com", "street_address": "1600 Amphitheatre Parkway", "zip_code": "94043",
    "city": "Mountain View", "state": "CA", "country": "United States",
    "credit_card_number": "4432801561520454", "credit_card_expiration_month": "1",
    "credit_card_expiration_year": "2039", "credit_card_cvv": "672",
}


@dataclass(slots=True)
class Sample:
    start: float          # wall-clock seconds
    end: float
    flow: str
    status: int | None    # HTTP status, None on connection error / timeout
    ok: bool
    error: str | None = None

    @property
    def latency_ms(self) -> float:
        return (self.end - self.start) * 1000.0


class Prober:
    def __init__(self, base_url: str, users: int = cfg.PROBE_USERS, think_s: float = cfg.PROBE_THINK_S,
                 timeout_s: float = cfg.PROBE_TIMEOUT_S) -> None:
        self.base_url = base_url.rstrip("/")
        self.users = users
        self.think_s = think_s
        self.timeout_s = timeout_s
        self.samples: list[Sample] = []
        self.last_response = time.time()   # last time any response (success or HTTP error) arrived
        self.started_at = time.time()

    async def run(self, stop: asyncio.Event) -> None:
        self.started_at = self.last_response = time.time()
        await asyncio.gather(*(self._user(i, stop) for i in range(self.users)))

    async def _user(self, idx: int, stop: asyncio.Event) -> None:
        await asyncio.sleep(idx * self.think_s / self.users)   # spread users out
        timeout = httpx.Timeout(self.timeout_s)
        headers = {"User-Agent": "chaos-agent-probe/1.0"}
        async with httpx.AsyncClient(base_url=self.base_url, timeout=timeout, headers=headers,
                                     follow_redirects=False) as http:
            while not stop.is_set():
                product = random.choice(PRODUCT_IDS)
                steps = [
                    ("home", "GET", "/", None),
                    ("product", "GET", f"/product/{product}", None),
                    ("add_to_cart", "POST", "/cart", {"product_id": product, "quantity": str(random.randint(1, 3))}),
                    ("view_cart", "GET", "/cart", None),
                    ("checkout", "POST", "/cart/checkout", CHECKOUT_FORM),
                ]
                for flow, method, path, data in steps:
                    if stop.is_set():
                        return
                    await self._request(http, flow, method, path, data)
                    try:
                        await asyncio.wait_for(stop.wait(), timeout=self.think_s)
                    except asyncio.TimeoutError:
                        pass

    async def _request(self, http: httpx.AsyncClient, flow: str, method: str, path: str, data: dict | None) -> None:
        start = time.time()
        try:
            resp = await http.request(method, path, data=data)
            end = time.time()
            self.last_response = end
            ok = resp.status_code < 400            # 302 after add-to-cart is success
            self.samples.append(Sample(start, end, flow, resp.status_code, ok,
                                       None if ok else f"HTTP {resp.status_code}"))
        except httpx.TimeoutException:
            self.samples.append(Sample(start, time.time(), flow, None, False, "timeout"))
        except httpx.HTTPError as e:
            self.samples.append(Sample(start, time.time(), flow, None, False, type(e).__name__))

    def window(self, now: float | None = None, window_s: float = cfg.WINDOW_S) -> list[Sample]:
        """Samples that completed in the last `window_s` seconds."""
        now = now or time.time()
        lo = now - window_s
        out = []
        for s in reversed(self.samples):
            if s.end < lo:
                break
            if s.end <= now:
                out.append(s)
        return out

    def between(self, t0: float, t1: float) -> list[Sample]:
        return [s for s in self.samples if t0 <= s.end <= t1]


# --- Statistics ------------------------------------------------------------------------------------------
def percentile(values: list[float], q: float) -> float | None:
    """Linear-interpolated percentile (q in 0..100)."""
    if not values:
        return None
    xs = sorted(values)
    k = (len(xs) - 1) * q / 100.0
    lo, hi = math.floor(k), math.ceil(k)
    return xs[lo] + (xs[hi] - xs[lo]) * (k - lo)


def stats(samples: list[Sample]) -> FlowStats:
    if not samples:
        return FlowStats()
    lat = [s.latency_ms for s in samples]
    errors = sum(1 for s in samples if not s.ok)
    r = lambda v: None if v is None else round(v, 1)
    return FlowStats(count=len(samples), errors=errors, error_rate=round(errors / len(samples), 4),
                     p50_ms=r(percentile(lat, 50)), p95_ms=r(percentile(lat, 95)), p99_ms=r(percentile(lat, 99)))


def per_flow(samples: list[Sample]) -> dict[str, FlowStats]:
    return {f: stats([s for s in samples if s.flow == f]) for f in FLOWS}
