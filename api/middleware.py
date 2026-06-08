from __future__ import annotations

import time
from collections import defaultdict
from typing import Callable

from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

from config import settings


class RateLimitMiddleware(BaseHTTPMiddleware):
    def __init__(self, app: Callable, rate_limit: int = settings.rate_limit_per_minute) -> None:
        super().__init__(app)
        self.rate_limit = rate_limit
        self.history: dict[str, list[float]] = defaultdict(list)

    async def dispatch(self, request: Request, call_next):
        client_ip = request.client.host if request.client else "unknown"
        now = time.time()
        window = 60.0
        timestamps = [ts for ts in self.history[client_ip] if ts > now - window]
        timestamps.append(now)
        self.history[client_ip] = timestamps
        if len(timestamps) > self.rate_limit:
            return JSONResponse(
                status_code=429,
                content={"detail": "Rate limit exceeded"},
            )
        return await call_next(request)
