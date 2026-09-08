#!/usr/bin/env python3
"""Standalone M1 server for isolated process memory and concurrency profiling."""

import asyncio
import os
import signal
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from hub.config import SecurityConfig, ServerConfig
from hub.models import HTTPRequest, HTTPResponse
from hub.security import validate_request_security
from hub.server import AsyncHTTPServer


async def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 19440
    secret = "adversarial_super_secret_key_32_bytes_len!"
    bearer = "adversarial_bearer_token_secret_32_chars!"

    srv_cfg = ServerConfig(host="127.0.0.1", port=port, max_body_bytes=1048576)
    sec_cfg = SecurityConfig(
        auth_mode="any",
        webhook_secret=secret,
        bearer_token=bearer,
        timestamp_tolerance_seconds=300,
        future_timestamp_tolerance_seconds=60,
    )

    server = AsyncHTTPServer(srv_cfg)

    @server.get("/healthz")
    def health_handler(req):
        return {"status": "ok"}

    @server.post("/webhook")
    async def webhook_handler(req: HTTPRequest):
        val = validate_request_security(req, sec_cfg)
        if not val.is_valid:
            return HTTPResponse.error(val.message, status_code=val.status_code, reason=val.reason)
        return HTTPResponse.json({"status": "accepted", "event_id": "evt_test"}, status_code=202)

    @server.post("/webhook/{source}")
    async def webhook_source_handler(req: HTTPRequest, source: str):
        val = validate_request_security(req, sec_cfg)
        if not val.is_valid:
            return HTTPResponse.error(val.message, status_code=val.status_code, reason=val.reason)
        return HTTPResponse.json({"status": "accepted", "source": source, "event_id": "evt_test"}, status_code=202)

    @server.get("/events/stream")
    async def sse_handler(req: HTTPRequest):
        async def event_generator():
            for i in range(5):
                yield f"event: test\ndata: {{\"i\": {i}}}\n\n"
                await asyncio.sleep(0.01)
        return HTTPResponse.stream(event_generator())

    await server.start()
    print("READY", flush=True)

    stop_event = asyncio.Event()

    def handle_signal():
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, handle_signal)

    await stop_event.wait()
    await server.stop()


if __name__ == "__main__":
    asyncio.run(main())
