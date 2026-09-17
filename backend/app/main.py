"""SnapChart backend - ingest API + chair-side UI host.

Run with:  python -m uvicorn app.main:app --reload   (from backend/)
or:        python run_server.py
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles

from .config import settings
from .db import init_db
from .events import broker
from .routers import bridge, images, ingest, mobile, patients, sessions

KEEPALIVE_SECONDS = 15


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    # Ingest runs on worker threads; the broker needs a handle on the loop that
    # owns the SSE subscriber queues in order to hand events across.
    broker.bind_loop(asyncio.get_running_loop())
    yield


app = FastAPI(
    title="SnapChart ingest API",
    version="1.0.0",
    summary="Automated DSLR-to-chart capture workflow for dental practices",
    lifespan=lifespan,
)

# The UI is served from this same origin by default. CORS stays permissive only
# for localhost so the UI can also be opened from a live-server during dev.
app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"http://(localhost|127\.0\.0\.1)(:\d+)?",
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(ingest.router)
app.include_router(patients.router)
app.include_router(sessions.router)
app.include_router(images.router)
app.include_router(bridge.router)
app.include_router(mobile.router)


@app.get("/api/health", tags=["meta"])
def health():
    return {"status": "ok", "service": "snapchart-backend"}


@app.get("/api/events", tags=["meta"])
async def events(request: Request):
    """Server-sent events: every capture appears in the UI as it is charted.

    SSE rather than WebSockets because the traffic is strictly one-way and SSE
    reconnects on its own after a dropped Wi-Fi link - which is exactly the
    failure mode a dental operatory has.
    """
    queue = broker.subscribe()

    async def stream():
        try:
            yield "retry: 3000\n\n"
            while True:
                if await request.is_disconnected():
                    break
                try:
                    payload = await asyncio.wait_for(queue.get(), timeout=KEEPALIVE_SECONDS)
                except asyncio.TimeoutError:
                    yield ": keep-alive\n\n"
                    continue
                yield f"data: {payload}\n\n"
        finally:
            broker.unsubscribe(queue)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# Mounted last so it never shadows an /api route.
if settings.serve_frontend and settings.frontend_dir.is_dir():
    app.mount("/", StaticFiles(directory=settings.frontend_dir, html=True), name="ui")
