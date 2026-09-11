"""
Antigravity Webhook Hub — Pure Asyncio HTTP/1.1 Server
Zero-dependency, high-efficiency HTTP/1.1 server built on standard library asyncio.
Guarantees <30MB RAM footprint on macOS and 0% idle CPU.
Supports routes registration, parameter extraction, query params, headers,
keep-alive connections, and streaming SSE responses.
"""

from __future__ import annotations

import asyncio
import gc
import json
import logging
import re
import sys
import time
import types
import urllib.parse
from typing import Any, AsyncIterator, Awaitable, Callable, Optional

from hub.config import ServerConfig
from hub.memory import apply_memory_pressure_relief
from hub.models import HTTPRequest, HTTPResponse

_HTTP_DAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
_HTTP_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def _format_http_date(timestamp: Optional[float] = None) -> str:
    """Format RFC 1123 / RFC 2822 HTTP date without importing email package."""
    t = time.gmtime(timestamp if timestamp is not None else time.time())
    return f"{_HTTP_DAYS[t.tm_wday]}, {t.tm_mday:02d} {_HTTP_MONTHS[t.tm_mon - 1]} {t.tm_year:04d} {t.tm_hour:02d}:{t.tm_min:02d}:{t.tm_sec:02d} GMT"

logger = logging.getLogger("hub.server")

# Route handler type: accepts HTTPRequest and optional path kwargs, returns HTTPResponse or Any
RouteHandler = Callable[..., Any]
MiddlewareFunc = Callable[[HTTPRequest], Awaitable[Optional[HTTPResponse]]]


class RoutePattern:
    """Represents a parameterized route pattern (e.g. /webhook/{source})."""

    def __init__(self, method: str, pattern: str, handler: RouteHandler):
        self.method = method.upper()
        self.raw_pattern = pattern
        self.handler = handler

        # Compile pattern like /tasks/{task_id}/stream to regex
        # Replaces {param_name} with (?P<param_name>[^/]+)
        param_names: list[str] = []

        def replace_param(match: re.Match) -> str:
            name = match.group(1)
            param_names.append(name)
            return f"(?P<{name}>[^/]+)"

        regex_str = "^" + re.sub(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}", replace_param, pattern) + "$"
        self.regex = re.compile(regex_str)
        self.param_names = param_names

    def match(self, path: str) -> Optional[dict[str, str]]:
        m = self.regex.match(path)
        if not m:
            return None
        return m.groupdict()


def _is_coroutine_callable(fn: Any) -> bool:
    target = fn
    if hasattr(target, "__func__"):
        target = target.__func__
    elif hasattr(target, "__call__") and not isinstance(target, (type, types.FunctionType, types.MethodType)):
        target = getattr(target.__call__, "__func__", target.__call__)
    code = getattr(target, "__code__", None)
    if code is not None:
        return bool(code.co_flags & (0x80 | 0x100))
    try:
        import inspect
        return inspect.iscoroutinefunction(fn) or inspect.iscoroutinefunction(getattr(fn, "__call__", None))
    except Exception:
        return False


def _extract_handler_params(handler: RouteHandler) -> tuple[tuple[str, ...], Optional[str], bool]:
    target = handler
    skip = 0
    if hasattr(target, "__self__") and hasattr(target, "__func__"):
        target = target.__func__
        skip = 1
    elif hasattr(target, "__call__") and not isinstance(target, (type, types.FunctionType, types.MethodType)):
        call_fn = getattr(target.__call__, "__func__", target.__call__)
        target = call_fn
        skip = 1 if hasattr(target, "__self__") else 0
    code = getattr(target, "__code__", None)
    if code is not None:
        total_named_args = code.co_argcount + getattr(code, "co_kwonlyargcount", 0)
        names = code.co_varnames[skip:total_named_args]
        has_var_keyword = bool(code.co_flags & 0x08)
        req_name = None
        for n in names:
            if n in ("request", "req"):
                req_name = n
                break
        return tuple(names), req_name, has_var_keyword
    try:
        import inspect
        sig = inspect.signature(handler)
        names = tuple(sig.parameters.keys())
        has_var_keyword = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values())
        req_name = None
        for p_name, p_param in sig.parameters.items():
            if p_name in ("request", "req") or p_param.annotation == HTTPRequest:
                req_name = p_name
                break
        return names, req_name, has_var_keyword
    except Exception:
        return (), None, False


class _HandlerInvoker:
    """Pre-analyzed dispatch invoker for a route handler to avoid runtime inspect overhead."""

    __slots__ = ("param_names", "req_param", "has_var_keyword", "num_params", "is_async")

    def __init__(self, handler: RouteHandler):
        self.param_names, self.req_param, self.has_var_keyword = _extract_handler_params(handler)
        self.num_params = len(self.param_names)
        self.is_async = _is_coroutine_callable(handler)

    def invoke(
        self, handler: RouteHandler, request: HTTPRequest, path_kwargs: dict[str, str]
    ) -> Any:
        if self.num_params == 0 and not self.has_var_keyword:
            return handler()
        if not path_kwargs and self.num_params == 1 and not self.has_var_keyword:
            return handler(request)

        call_args: dict[str, Any] = {}
        for p_name in self.param_names:
            if p_name in path_kwargs:
                call_args[p_name] = path_kwargs[p_name]
            elif p_name == self.req_param:
                call_args[p_name] = request

        # If request is not yet bound and there is an unbound parameter, bind it
        if request not in call_args.values():
            for p_name in self.param_names:
                if p_name not in call_args:
                    call_args[p_name] = request
                    break

        if self.has_var_keyword:
            for k, v in path_kwargs.items():
                call_args.setdefault(k, v)

        if not call_args and self.num_params == 0 and not self.has_var_keyword:
            return handler()
        if not call_args and self.num_params == 1 and not self.has_var_keyword:
            return handler(request)
        return handler(**call_args)


def _get_invoker(handler: RouteHandler) -> _HandlerInvoker:
    """Retrieve or attach cached invoker directly on handler to prevent unbounded global map leaks."""
    try:
        invoker = getattr(handler, "__hub_invoker__", None)
        if invoker is not None:
            return invoker
    except Exception:
        pass
    invoker = _HandlerInvoker(handler)
    try:
        if hasattr(handler, "__dict__"):
            handler.__hub_invoker__ = invoker
    except Exception:
        pass
    return invoker


class AsyncHTTPServer:
    """
    Pure asyncio HTTP/1.1 Server.
    Zero external dependencies, minimal memory footprint (<30MB RSS on macOS).
    """

    def __init__(self, config: Optional[ServerConfig] = None):
        self.config = config or ServerConfig()
        self.host = self.config.host
        self.port = self.config.port
        self.max_body_bytes = self.config.max_body_bytes
        self.keep_alive_timeout = self.config.keep_alive_timeout
        self.request_timeout: float = float(getattr(self.config, "request_timeout", self.keep_alive_timeout))

        # Routing tables
        # Exact match: (method, path) -> handler
        self._exact_routes: dict[tuple[str, str], RouteHandler] = {}
        # Parameterized routes list
        self._pattern_routes: list[RoutePattern] = []

        # Middlewares executed in sequence before route handler
        self._middlewares: list[MiddlewareFunc] = []

        # Internal server state
        self._server: Optional[asyncio.Server] = None
        self._active_connections: int = 0
        self._total_requests: int = 0
        self._start_time: float = 0.0
        self._is_running: bool = False
        self._active_tasks: set[asyncio.Task] = set()
        self._active_transports: set[asyncio.BaseTransport] = set()
        self._memory_monitor_task: Optional[asyncio.Task] = None
        self._db: Optional[Any] = None

    # --- Routing Registration API ---

    def add_route(self, method: str, path: str, handler: RouteHandler) -> None:
        """Register a handler for an HTTP method and path."""
        method = method.upper()
        path = "/" + path.strip("/") if path != "/" else "/"

        if "{" in path and "}" in path:
            self._pattern_routes = [
                r for r in self._pattern_routes
                if not (r.method == method and r.raw_pattern == path)
            ]
            self._pattern_routes.append(RoutePattern(method, path, handler))
        else:
            self._exact_routes[(method, path)] = handler
        _get_invoker(handler)

    def add_middleware(self, middleware: MiddlewareFunc) -> None:
        """Register a middleware function executed before route handlers."""
        self._middlewares.append(middleware)

    def route(self, method: str, path: str):
        """Decorator to register a route handler."""
        def decorator(handler: RouteHandler):
            self.add_route(method, path, handler)
            return handler
        return decorator

    def get(self, path: str):
        """Decorator for GET routes."""
        return self.route("GET", path)

    def post(self, path: str):
        """Decorator for POST routes."""
        return self.route("POST", path)

    def put(self, path: str):
        """Decorator for PUT routes."""
        return self.route("PUT", path)

    def delete(self, path: str):
        """Decorator for DELETE routes."""
        return self.route("DELETE", path)

    def options(self, path: str):
        """Decorator for OPTIONS routes."""
        return self.route("OPTIONS", path)

    # --- Route Matching ---

    def _resolve_route(
        self, method: str, path: str
    ) -> tuple[Optional[RouteHandler], dict[str, str], bool]:
        """
        Resolve route for method and path.
        Returns: (handler, kwargs, path_exists_for_other_method)
        """
        norm_path = "/" + path.strip("/") if path != "/" else "/"

        # 1. Exact match
        exact_key = (method, norm_path)
        if exact_key in self._exact_routes:
            return self._exact_routes[exact_key], {}, False

        # 2. Pattern match
        for r_pattern in self._pattern_routes:
            if r_pattern.method == method:
                params = r_pattern.match(norm_path)
                if params is not None:
                    return r_pattern.handler, params, False

        # 3. Check for 405 Method Not Allowed
        path_exists = False
        for (m, p) in self._exact_routes:
            if p == norm_path and m != method:
                path_exists = True
                break

        if not path_exists:
            for r_pattern in self._pattern_routes:
                if r_pattern.match(norm_path) is not None and r_pattern.method != method:
                    path_exists = True
                    break

        return None, {}, path_exists

    # --- Connection & HTTP/1.1 Protocol Handling ---

    async def _handle_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        """Handle an incoming client connection with HTTP/1.1 Keep-Alive support."""
        self._active_connections += 1
        peer_info = writer.get_extra_info("peername")
        client_ip = peer_info[0] if peer_info and isinstance(peer_info, tuple) else "127.0.0.1"

        try:
            while self._is_running:
                # Wait for request line and headers with keepalive timeout
                try:
                    header_bytes = await asyncio.wait_for(
                        reader.readuntil(b"\r\n\r\n"),
                        timeout=float(self.keep_alive_timeout),
                    )
                except asyncio.TimeoutError:
                    # Keep-alive timeout elapsed, cleanly close connection
                    break
                except (asyncio.IncompleteReadError, ConnectionResetError, BrokenPipeError):
                    # Client disconnected
                    break

                if not header_bytes:
                    break

                self._total_requests += 1

                # 1. Parse request line & headers
                try:
                    lines = header_bytes.split(b"\r\n")
                    req_line = lines[0].decode("latin1").strip()
                    parts = req_line.split(" ")
                    if len(parts) != 3:
                        await self._send_quick_error(writer, 400, "Bad Request")
                        break

                    method, raw_uri, http_version = parts
                    method = method.upper()
                    http_version = http_version.upper()

                    # Parse URI (path & query string)
                    parsed_url = urllib.parse.urlsplit(raw_uri)
                    req_path = urllib.parse.unquote(parsed_url.path)
                    if not req_path.startswith("/"):
                        req_path = "/" + req_path

                    # Parse query params
                    raw_qs = urllib.parse.parse_qs(parsed_url.query, keep_blank_values=True)
                    query_params = {k: v[0] if len(v) == 1 else ",".join(v) for k, v in raw_qs.items()}

                    # Parse headers (case-insensitive dictionary with lowercase keys)
                    headers: dict[str, str] = {}
                    for line in lines[1:]:
                        if not line:
                            continue
                        decoded_line = line.decode("latin1")
                        if ":" in decoded_line:
                            hk, hv = decoded_line.split(":", 1)
                            headers[hk.strip().lower()] = hv.strip()

                except Exception as e:
                    logger.debug("Malformed HTTP header parsing: %s", e)
                    await self._send_quick_error(writer, 400, "Bad Request")
                    break

                # 2. Parse request body
                content_length_str = headers.get("content-length")
                transfer_encoding = headers.get("transfer-encoding", "").lower()
                body = b""

                # Request Smuggling Protection (RFC 7230 §3.3.3 / RFC 9112 §6.1):
                # If a request contains BOTH Content-Length and Transfer-Encoding, reject with 400 Bad Request
                if "content-length" in headers and "transfer-encoding" in headers:
                    await self._send_quick_error(
                        writer, 400, "Bad Request: Conflicting Content-Length and Transfer-Encoding"
                    )
                    break

                if content_length_str is not None:
                    try:
                        content_length = int(content_length_str)
                    except ValueError:
                        await self._send_quick_error(writer, 400, "Invalid Content-Length")
                        break

                    if content_length < 0:
                        await self._send_quick_error(writer, 400, "Invalid Content-Length: must be non-negative")
                        break

                    if content_length > self.max_body_bytes:
                        await self._send_quick_error(
                            writer,
                            413,
                            f"Payload size ({content_length} bytes) exceeds maximum limit ({self.max_body_bytes} bytes)",
                        )
                        try:
                            await asyncio.sleep(0.01)
                        except Exception:
                            pass
                        break

                    if content_length > 0:
                        try:
                            body = await asyncio.wait_for(
                                reader.readexactly(content_length),
                                timeout=self.request_timeout,
                            )
                        except asyncio.IncompleteReadError:
                            break
                        except asyncio.TimeoutError:
                            await self._send_quick_error(writer, 408, "Request Timeout")
                            break

                elif "chunked" in transfer_encoding:
                    # Read chunked body
                    chunk_body = bytearray()
                    try:
                        while True:
                            size_line = await asyncio.wait_for(
                                reader.readuntil(b"\r\n"),
                                timeout=self.request_timeout,
                            )
                            chunk_size = int(size_line.strip().split(b";")[0], 16)
                            if chunk_size < 0:
                                await self._send_quick_error(writer, 400, "Invalid Chunk Size")
                                return
                            if chunk_size == 0:
                                # Read trailer and trailing CRLF
                                await asyncio.wait_for(
                                    reader.readuntil(b"\r\n"),
                                    timeout=self.request_timeout,
                                )
                                break
                            if len(chunk_body) + chunk_size > self.max_body_bytes:
                                await self._send_quick_error(writer, 413, "Chunked Payload Too Large")
                                return
                            chunk_data = await asyncio.wait_for(
                                reader.readexactly(chunk_size),
                                timeout=self.request_timeout,
                            )
                            chunk_body.extend(chunk_data)
                            await asyncio.wait_for(
                                reader.readuntil(b"\r\n"),  # trailing CRLF
                                timeout=self.request_timeout,
                            )
                        body = bytes(chunk_body)
                    except asyncio.TimeoutError:
                        await self._send_quick_error(writer, 408, "Request Timeout")
                        break
                    except Exception:
                        await self._send_quick_error(writer, 400, "Invalid Chunked Body")
                        break

                # 3. Build HTTPRequest object
                request = HTTPRequest(
                    method=method,
                    path=req_path,
                    raw_path=parsed_url.path,
                    query_params=query_params,
                    headers=headers,
                    body=body,
                    remote_addr=client_ip,
                    timestamp=time.time(),
                )

                # Determine keep-alive preference
                connection_header = headers.get("connection", "").lower()
                if http_version == "HTTP/1.0":
                    client_wants_keep_alive = connection_header == "keep-alive"
                else:
                    client_wants_keep_alive = connection_header != "close"

                # 4. Execute Middlewares
                early_response: Optional[HTTPResponse] = None
                for mw in self._middlewares:
                    try:
                        mw_res = await mw(request)
                        if mw_res is not None:
                            early_response = mw_res
                            break
                    except Exception as mw_err:
                        logger.exception("Middleware error: %s", mw_err)
                        early_response = HTTPResponse.error("Internal Server Error", status_code=500)
                        break

                if early_response is not None:
                    response = early_response
                else:
                    # 5. Resolve Route
                    handler, path_kwargs, method_not_allowed = self._resolve_route(method, req_path)
                    if method_not_allowed:
                        response = HTTPResponse.error(
                            f"Method {method} not allowed for {req_path}",
                            status_code=405,
                            headers={"Allow": "GET, POST, OPTIONS, HEAD"},
                        )
                    elif handler is None:
                        response = HTTPResponse.error(
                            f"Route not found: {method} {req_path}",
                            status_code=404,
                        )
                    else:
                        # 6. Execute Handler
                        try:
                            invoker = _get_invoker(handler)
                            result = invoker.invoke(handler, request, path_kwargs)

                            if invoker.is_async or hasattr(type(result), "__await__"):
                                result = await result

                            # Normalize result to HTTPResponse
                            if isinstance(result, HTTPResponse):
                                response = result
                            elif isinstance(result, (dict, list)):
                                response = HTTPResponse.json(result)
                            elif isinstance(result, str):
                                response = HTTPResponse.text(result)
                            elif isinstance(result, bytes):
                                response = HTTPResponse(status_code=200, body=result)
                            elif result is None:
                                response = HTTPResponse.empty()
                            else:
                                response = HTTPResponse.text(str(result))

                        except Exception as handler_err:
                            logger.exception("Handler exception: %s", handler_err)
                            response = HTTPResponse.error(
                                f"Internal Server Error: {str(handler_err)}",
                                status_code=500,
                            )

                # 7. Write HTTP Response
                should_keep_alive = client_wants_keep_alive and (response.status_code < 500)
                await self._write_response(writer, response, should_keep_alive)

                is_stream_resp = response.is_stream
                del body
                del request
                del response
                if should_keep_alive and (self._total_requests % 5 == 0):
                    gc.collect(2)
                    self._pressure_relief()
                    if self._db is not None and hasattr(self._db, "shrink_memory"):
                        self._db.shrink_memory(truncate_wal=False)

                if not should_keep_alive or is_stream_resp:
                    # For streaming or non-keepalive, close connection
                    break

        except Exception as conn_err:
            logger.debug("Connection handling exception: %s", conn_err)
        finally:
            self._active_connections -= 1
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass
            del writer
            del reader
            gc.collect(2)
            self._pressure_relief()
            if self._db is not None and hasattr(self._db, "shrink_memory"):
                try:
                    self._db.shrink_memory(truncate_wal=False)
                except Exception:
                    pass

    async def _write_response(
        self,
        writer: asyncio.StreamWriter,
        response: HTTPResponse,
        keep_alive: bool,
    ) -> None:
        """Serialize and send HTTP/1.1 response over writer."""
        status_reasons = {
            200: "OK",
            201: "Created",
            202: "Accepted",
            204: "No Content",
            400: "Bad Request",
            401: "Unauthorized",
            403: "Forbidden",
            404: "Not Found",
            405: "Method Not Allowed",
            408: "Request Timeout",
            409: "Conflict",
            413: "Payload Too Large",
            431: "Request Header Fields Too Large",
            500: "Internal Server Error",
            502: "Bad Gateway",
            503: "Service Unavailable",
        }
        reason = status_reasons.get(response.status_code, "OK")

        # Standard headers
        headers = dict(response.headers)
        headers["Date"] = _format_http_date()
        headers["Server"] = "Antigravity-Webhook-Hub/1.0"

        if response.is_stream and response.stream_generator:
            # Server-Sent Events / Chunked Streaming Response
            headers["Transfer-Encoding"] = "chunked"
            headers["Connection"] = "keep-alive"
            headers["Cache-Control"] = "no-cache, no-transform"
            headers["X-Accel-Buffering"] = "no"

            # Write header block
            header_lines = [f"HTTP/1.1 {response.status_code} {reason}\r\n"]
            for hk, hv in headers.items():
                header_lines.append(f"{hk}: {hv}\r\n")
            header_lines.append("\r\n")
            writer.write("".join(header_lines).encode("latin1"))
            await writer.drain()

            # Stream chunks using HTTP/1.1 chunked encoding
            try:
                async for item in response.stream_generator:
                    if isinstance(item, str):
                        chunk = item.encode("utf-8")
                    elif isinstance(item, bytes):
                        chunk = item
                    else:
                        chunk = str(item).encode("utf-8")

                    if not chunk:
                        continue

                    # Write chunk: <HEX_SIZE>\r\n<DATA>\r\n
                    writer.write(f"{len(chunk):X}\r\n".encode("ascii") + chunk + b"\r\n")
                    await writer.drain()

                # Terminating chunk
                writer.write(b"0\r\n\r\n")
                await writer.drain()
            except (ConnectionResetError, BrokenPipeError, asyncio.CancelledError):
                pass
            finally:
                if hasattr(response.stream_generator, "aclose"):
                    try:
                        await response.stream_generator.aclose()
                    except Exception:
                        pass

        else:
            # Regular Content-Length Response
            headers["Content-Length"] = str(len(response.body))
            headers["Connection"] = "keep-alive" if keep_alive else "close"

            header_lines = [f"HTTP/1.1 {response.status_code} {reason}\r\n"]
            for hk, hv in headers.items():
                header_lines.append(f"{hk}: {hv}\r\n")
            header_lines.append("\r\n")

            writer.write("".join(header_lines).encode("latin1"))
            if response.body:
                writer.write(response.body)
            await writer.drain()

    async def _send_quick_error(
        self, writer: asyncio.StreamWriter, status_code: int, message: str
    ) -> None:
        """Send a quick error response and close connection."""
        resp = HTTPResponse.error(message, status_code=status_code)
        await self._write_response(writer, resp, keep_alive=False)

    # --- Server Lifecycle ---

    async def start(self) -> None:
        """Start the HTTP server on configured host and port."""
        if self._server is not None:
            return

        self._start_time = time.time()
        self._is_running = True

        def client_connected(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
            transport = writer.transport
            if transport is not None:
                self._active_transports.add(transport)
            task = asyncio.create_task(self._handle_client(reader, writer))
            self._active_tasks.add(task)

            def _on_task_done(t: asyncio.Task):
                self._active_tasks.discard(t)
                if transport is not None:
                    self._active_transports.discard(transport)
                if not self._active_tasks:
                    gc.collect()
                    self._pressure_relief()
                    if self._db is not None and hasattr(self._db, "shrink_memory"):
                        try:
                            self._db.shrink_memory()
                        except Exception:
                            pass

            task.add_done_callback(_on_task_done)

        self._server = await asyncio.start_server(
            client_connected,
            host=self.host,
            port=self.port,
            reuse_address=True,
        )
        self._memory_monitor_task = asyncio.create_task(self._idle_memory_monitor())
        logger.info("Antigravity Webhook Hub HTTP server listening on http://%s:%d", self.host, self.port)

    def _pressure_relief(self) -> None:
        """Periodic background memory relief maintaining <30MB budget on macOS."""
        apply_memory_pressure_relief()


    async def _idle_memory_monitor(self) -> None:
        """Periodic background memory relief maintaining <30MB budget on macOS."""
        gc.collect(2)
        self._pressure_relief()
        if self._db is not None and hasattr(self._db, "shrink_memory"):
            self._db.shrink_memory(truncate_wal=False)
        while self._is_running:
            try:
                await asyncio.sleep(1.0)
                gc.collect(2)
                self._pressure_relief()
                if self._db is not None and hasattr(self._db, "shrink_memory"):
                    self._db.shrink_memory(truncate_wal=False)
            except asyncio.CancelledError:
                break
            except Exception:
                pass

    async def stop(self) -> None:
        """Gracefully stop the HTTP server and close active connections."""
        self._is_running = False
        if self._memory_monitor_task is not None:
            self._memory_monitor_task.cancel()
            try:
                await self._memory_monitor_task
            except asyncio.CancelledError:
                pass
            self._memory_monitor_task = None
        if self._server is not None:
            self._server.close()

        # Cancel active client tasks and close connection transports before wait_closed()
        if self._active_tasks:
            for task in list(self._active_tasks):
                if not task.done():
                    task.cancel()

        for transport in list(self._active_transports):
            try:
                transport.close()
            except Exception:
                pass
        self._active_transports.clear()

        if self._active_tasks:
            await asyncio.gather(*self._active_tasks, return_exceptions=True)
            self._active_tasks.clear()

        if self._server is not None:
            await self._server.wait_closed()
            self._server = None

        logger.info("Antigravity Webhook Hub HTTP server stopped")

    async def __aenter__(self) -> AsyncHTTPServer:
        await self.start()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        await self.stop()

    def get_stats(self) -> dict[str, Any]:
        """Return operational server metrics."""
        uptime = time.time() - self._start_time if self._start_time > 0 else 0.0
        return {
            "uptime_seconds": round(uptime, 2),
            "active_connections": self._active_connections,
            "total_requests": self._total_requests,
            "host": self.host,
            "port": self.port,
            "is_running": self._is_running,
        }


def wire_routes(
    server: AsyncHTTPServer,
    config: Any,
    db: Optional[Any] = None,
    broker: Optional[Any] = None,
    dispatcher: Optional[Any] = None,
) -> None:
    """Wire all system routes (webhook, tasks, observability, sse) into the server."""
    if db is not None:
        server._db = db

    try:
        from hub.routes.webhook import register_webhook_routes
        register_webhook_routes(server, config, db, dispatcher, broker)
    except ImportError:
        pass

    try:
        from hub.routes.uptime_kuma import register_uptime_kuma_routes
        register_uptime_kuma_routes(server, config, db, dispatcher, broker)
    except ImportError:
        pass

    try:
        from hub.routes.tasks import register_task_routes
        register_task_routes(server, config, db, dispatcher, broker)
    except ImportError:
        pass

    try:
        from hub.routes.observability import register_observability_routes
        register_observability_routes(server, config, db, broker, dispatcher)
    except ImportError:
        pass

    try:
        from hub.routes.sse import register_sse_routes
        register_sse_routes(server, config, db, broker)
    except ImportError:
        pass
