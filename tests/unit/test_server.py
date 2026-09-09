"""Unit tests for hub.server pure asyncio HTTP/1.1 server."""

import asyncio
import json
import pytest

from hub.config import ServerConfig
from hub.models import HTTPResponse
from hub.server import AsyncHTTPServer


@pytest.fixture
def server_port():
    return 19425


@pytest.mark.anyio
async def test_server_lifecycle_and_routes(server_port):
    cfg = ServerConfig(host="127.0.0.1", port=server_port, max_body_bytes=2048)
    server = AsyncHTTPServer(cfg)

    @server.get("/healthz")
    def health_handler(request):
        return {"status": "ok"}

    @server.post("/webhook/{source}")
    async def webhook_handler(request, source: str):
        payload = request.json()
        return HTTPResponse.json({"status": "accepted", "source": source, "received": payload}, status_code=202)

    @server.get("/stream")
    async def stream_handler(request):
        async def event_generator():
            yield "event: ping\ndata: 1\n\n"
            yield "event: ping\ndata: 2\n\n"
        return HTTPResponse.stream(event_generator())

    await server.start()
    try:
        # 1. Test GET /healthz
        reader, writer = await asyncio.open_connection("127.0.0.1", server_port)
        writer.write(b"GET /healthz HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n")
        await writer.drain()

        header_data = await reader.readuntil(b"\r\n\r\n")
        assert b"200 OK" in header_data
        body_len = int([l for l in header_data.decode().split("\r\n") if l.lower().startswith("content-length:")][0].split(":")[1].strip())
        body = await reader.readexactly(body_len)
        data = json.loads(body.decode())
        assert data["status"] == "ok"

        # 2. Test POST /webhook/github with keep-alive
        req_payload = json.dumps({"action": "push"}).encode("utf-8")
        writer.write(
            f"POST /webhook/github HTTP/1.1\r\nHost: 127.0.0.1\r\nContent-Length: {len(req_payload)}\r\nContent-Type: application/json\r\n\r\n".encode("latin1")
            + req_payload
        )
        await writer.drain()

        header_data2 = await reader.readuntil(b"\r\n\r\n")
        assert b"202 Accepted" in header_data2
        body_len2 = int([l for l in header_data2.decode().split("\r\n") if l.lower().startswith("content-length:")][0].split(":")[1].strip())
        body2 = await reader.readexactly(body_len2)
        data2 = json.loads(body2.decode())
        assert data2["source"] == "github"
        assert data2["received"]["action"] == "push"

        # 3. Test 404 Route Not Found
        writer.write(b"GET /unknown_endpoint HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n")
        await writer.drain()
        header_data3 = await reader.readuntil(b"\r\n\r\n")
        assert b"404 Not Found" in header_data3
        body_len3 = int([l for l in header_data3.decode().split("\r\n") if l.lower().startswith("content-length:")][0].split(":")[1].strip())
        await reader.readexactly(body_len3)

        # 4. Test 405 Method Not Allowed
        writer.write(b"DELETE /healthz HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n")
        await writer.drain()
        header_data4 = await reader.readuntil(b"\r\n\r\n")
        assert b"405 Method Not Allowed" in header_data4
        body_len4 = int([l for l in header_data4.decode().split("\r\n") if l.lower().startswith("content-length:")][0].split(":")[1].strip())
        await reader.readexactly(body_len4)

        # 5. Test 413 Payload Too Large
        writer.write(b"POST /webhook/github HTTP/1.1\r\nHost: 127.0.0.1\r\nContent-Length: 5000\r\n\r\n")
        await writer.drain()
        header_data5 = await reader.readuntil(b"\r\n\r\n")
        assert b"413 Payload Too Large" in header_data5

        writer.close()
        await writer.wait_closed()

        # 6. Test SSE chunked streaming
        r_str, w_str = await asyncio.open_connection("127.0.0.1", server_port)
        w_str.write(b"GET /stream HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n")
        await w_str.drain()

        stream_headers = await r_str.readuntil(b"\r\n\r\n")
        assert b"200 OK" in stream_headers
        assert b"Transfer-Encoding: chunked" in stream_headers or b"transfer-encoding: chunked" in stream_headers

        accumulated = b""
        while True:
            chunk_line = await r_str.readuntil(b"\r\n")
            sz = int(chunk_line.strip(), 16)
            if sz == 0:
                await r_str.readuntil(b"\r\n")
                break
            chunk_bytes = await r_str.readexactly(sz)
            await r_str.readuntil(b"\r\n")
            accumulated += chunk_bytes

        assert b"data: 1" in accumulated and b"data: 2" in accumulated
        w_str.close()
        await w_str.wait_closed()

        # Check stats
        stats = server.get_stats()
        assert stats["total_requests"] >= 5
        assert stats["is_running"] is True

    finally:
        await server.stop()


@pytest.mark.anyio
async def test_server_kwonly_and_varkw_handlers(server_port):
    port = server_port + 1
    cfg = ServerConfig(host="127.0.0.1", port=port)
    server = AsyncHTTPServer(cfg)

    # 1. Handler with keyword-only arguments
    @server.get("/items/{item_id}")
    def kwonly_handler(req, *, item_id: str):
        return {"item_id": item_id}

    # 2. Handler with **kwargs
    @server.get("/custom/{foo}/{bar}")
    def varkw_handler(req, **kwargs):
        return {"captured": kwargs}

    # 3. Callable class instance handler
    class CallableAsyncHandler:
        async def __call__(self, request):
            return {"callable": "worked"}

    server.add_route("GET", "/callable", CallableAsyncHandler())

    await server.start()
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)

        # Test kwonly
        writer.write(b"GET /items/item_42 HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n")
        await writer.drain()
        headers = await reader.readuntil(b"\r\n\r\n")
        assert b"200 OK" in headers
        body_len = int([l for l in headers.decode().split("\r\n") if l.lower().startswith("content-length:")][0].split(":")[1].strip())
        body = json.loads((await reader.readexactly(body_len)).decode())
        assert body["item_id"] == "item_42"

        # Test varkw
        writer.write(b"GET /custom/hello/world HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n")
        await writer.drain()
        headers = await reader.readuntil(b"\r\n\r\n")
        assert b"200 OK" in headers
        body_len = int([l for l in headers.decode().split("\r\n") if l.lower().startswith("content-length:")][0].split(":")[1].strip())
        body = json.loads((await reader.readexactly(body_len)).decode())
        assert body["captured"] == {"foo": "hello", "bar": "world"}

        # Test callable object
        writer.write(b"GET /callable HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n")
        await writer.drain()
        headers = await reader.readuntil(b"\r\n\r\n")
        assert b"200 OK" in headers
        body_len = int([l for l in headers.decode().split("\r\n") if l.lower().startswith("content-length:")][0].split(":")[1].strip())
        body = json.loads((await reader.readexactly(body_len)).decode())
        assert body["callable"] == "worked"

        writer.close()
        await writer.wait_closed()
    finally:
        await server.stop()
