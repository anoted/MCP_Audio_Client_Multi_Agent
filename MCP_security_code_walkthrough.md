# MCP security code walkthrough — raw code and how each piece works

Companion to `MCP_security.md` (the *why* and the standards). This file is the
*how*: every security mechanism added for the streamable-HTTP Canvas MCP
server, with the actual code and an explanation of what it does and what
attack it stops. Two files carry all of it:

- **server side** — `examples/Canvas_MCP/canvas_mcp_server.py` (`_HTTPGuard`
  middleware + hardened entrypoint)
- **client side** — `app/mcp_manager.py` (`${VAR}` secret expansion,
  fail-closed connect)

---

## 1. Server: the `_HTTPGuard` ASGI middleware

Every HTTP request to the server passes through this middleware **before** any
MCP protocol code runs. It is written as *pure ASGI* (a class with an
`async __call__(scope, receive, send)`) rather than Starlette's
`BaseHTTPMiddleware`, because pure ASGI passes streaming responses (the SSE
stream of streamable HTTP) straight through without buffering.

```python
class _HTTPGuard:
    """Pure ASGI middleware: Host/Origin allowlists + static bearer token."""

    def __init__(self, app, token: str | None, allowed_hosts: set[str],
                 allowed_origins: set[str]):
        self.app = app
        self.token = token  # None → auth explicitly disabled via env
        self.allowed_hosts = allowed_hosts
        self.allowed_origins = allowed_origins

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = {
            k.decode("latin-1").lower(): v.decode("latin-1")
            for k, v in scope.get("headers", [])
        }
        if headers.get("host", "") not in self.allowed_hosts:
            await self._deny(send, 403, "Host header not allowed", scope)
            return
        origin = headers.get("origin")
        if origin and origin not in self.allowed_origins:
            await self._deny(send, 403, "Origin not allowed", scope)
            return
        if self.token is not None:
            scheme, _, credential = headers.get("authorization", "").partition(" ")
            if scheme.lower() != "bearer" or not secrets.compare_digest(
                credential.strip(), self.token
            ):
                await self._deny(
                    send, 401, "missing or invalid bearer token", scope,
                    extra=((b"www-authenticate", b'Bearer error="invalid_token"'),),
                )
                return
        await self.app(scope, receive, send)
```

Piece by piece:

- **`scope["type"] != "http"`** — ASGI also delivers `lifespan` (startup/
  shutdown) and websocket events; only HTTP requests are guarded, everything
  else passes through untouched.
- **Header normalization** — ASGI headers arrive as a list of
  `(bytes, bytes)` pairs; they are decoded and lower-cased once so lookups
  like `headers.get("authorization")` are case-insensitive, as HTTP requires.
- **Host allowlist (DNS-rebinding defense).** The attack: a victim's browser
  visits `evil.example`, whose DNS answer is then re-pointed at `127.0.0.1`.
  The browser happily sends requests to "evil.example" that actually land on
  this local server — same-origin policy is satisfied from the browser's
  point of view, so scripts on the page can read the responses. The tell is
  that the HTTP `Host:` header still says `evil.example:8099`. Rejecting any
  Host not on the allowlist (`127.0.0.1:<port>`, `localhost:<port>`, plus
  `CANVAS_MCP_ALLOWED_HOSTS`) kills the attack before auth is even checked.
- **Origin allowlist** — second layer of the same defense: a browser making a
  cross-origin `fetch()` to the server attaches `Origin: https://evil.example`.
  Non-browser MCP clients (the voice client's `httpx`) send **no** Origin
  header, which is why the check is `if origin and ...` — absent is fine,
  *wrong* is a 403.
- **Bearer check.** `partition(" ")` splits `"Bearer <credential>"`;
  the scheme must be `bearer` (case-insensitive per RFC 9110). The credential
  is compared with **`secrets.compare_digest`**, not `==`: a naive string
  compare returns early at the first mismatched byte, so an attacker who can
  measure response times could recover the token byte-by-byte (a *timing
  oracle*). `compare_digest` takes the same time whether the first or the
  last byte differs.
- **`self.token is None`** only happens when the operator explicitly set
  `CANVAS_MCP_ALLOW_NO_AUTH=1` — auth is skipped but the Host/Origin checks
  above still apply.
- **Ordering matters**: Host/Origin (who is asking, structurally) is checked
  before the token (do they hold the secret), so a rebinding attempt is
  rejected even if it somehow replayed a valid token.

## 2. Server: the deny path — audited, spec-shaped rejections

```python
    async def _deny(self, send, status: int, reason: str, scope, extra=()):
        _audit({
            "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
            "event": "http_denied",
            "status": status,
            "reason": reason,
            "path": scope.get("path"),
            "client": (scope.get("client") or ("?",))[0],
        })
        body = json.dumps({"error": reason}).encode()
        await send({
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
                *extra,
            ],
        })
        await send({"type": "http.response.body", "body": body})
```

- **Every rejection is audited.** The same `_audit()` JSONL writer that logs
  tool calls records `http_denied` with status, reason, path, and client IP —
  so port-scanning or token-guessing shows up in
  `examples/Canvas_MCP/logs/canvas-mcp-*.jsonl`, the server-side witness that
  the host app cannot edit. What is *never* logged: the presented credential.
- **The response is built with raw ASGI sends** (`http.response.start` +
  `http.response.body`) because at this point the request is refused without
  ever entering Starlette routing.
- **`WWW-Authenticate: Bearer error="invalid_token"`** rides in via `extra`
  on 401s. That challenge header is what the MCP auth spec (following RFC
  6750/9728) requires a resource server to return so a compliant client
  knows *how* to authenticate — under full OAuth it would also carry the
  `resource_metadata` URL.

Observed behavior (smoke test against a live server):

| Request | Result |
|---|---|
| POST `/mcp`, no `Authorization` | `401` + `WWW-Authenticate: Bearer error="invalid_token"` |
| POST `/mcp`, `Authorization: Bearer wrong` | `401` |
| valid token, `Host: evil.example:8099` | `403` |
| valid token, `Origin: http://evil.example` | `403` |
| valid token, proper MCP `initialize` | `200` |

## 3. Server: fail-closed startup and token generation

The entrypoint refuses to expose the endpoint without a secret — a missing
config produces a hard error at startup, not a silently open server:

```python
    token: str | None = os.environ.get("CANVAS_MCP_AUTH_TOKEN", "").strip() or None
    if token is None:
        if os.environ.get("CANVAS_MCP_ALLOW_NO_AUTH", "").strip().lower() not in (
            "1", "true", "yes"
        ):
            print(
                "ERROR: CANVAS_MCP_AUTH_TOKEN is not set — the streamable HTTP "
                "endpoint requires a bearer token.\n"
                ...
                file=sys.stderr,
            )
            sys.exit(1)
        print("WARNING: running WITHOUT authentication (CANVAS_MCP_ALLOW_NO_AUTH=1) — "
              "anything that can reach this port can wield your Canvas token.",
              file=sys.stderr)
    if MCP_HOST not in ("127.0.0.1", "localhost", "::1"):
        print(f"WARNING: binding to {MCP_HOST} exposes the endpoint beyond this "
              "machine, and bearer tokens travel in cleartext without TLS — put "
              "the server behind an HTTPS reverse proxy (see MCP_security.md).",
              file=sys.stderr)
```

- **Fail closed, with an escape hatch that must be *asked for*.** Secure by
  default: doing the insecure thing requires typing
  `CANVAS_MCP_ALLOW_NO_AUTH=1`, and even then a warning states exactly what
  is being risked (the Canvas API token behind the endpoint).
- **The bind-address warning** exists because a bearer token over plain HTTP
  is readable by anyone on the network path. Localhost never leaves the
  machine; anything else needs TLS in front.

Token generation:

```python
    if cli.make_token:
        print(secrets.token_urlsafe(32))
        sys.exit(0)
```

`secrets.token_urlsafe(32)` draws **32 bytes (256 bits) from the OS
cryptographic RNG** and base64url-encodes them (~43 characters). That is far
beyond brute-force range, and using a generator command steers users away
from inventing weak, guessable tokens by hand.

The allowlists are assembled from safe defaults plus optional env extensions:

```python
def _csv_env(name: str) -> set[str]:
    return {v.strip() for v in os.environ.get(name, "").split(",") if v.strip()}

    allowed_hosts = {
        f"127.0.0.1:{MCP_PORT}",
        f"localhost:{MCP_PORT}",
        f"{MCP_HOST}:{MCP_PORT}",
    } | _csv_env("CANVAS_MCP_ALLOWED_HOSTS")
    allowed_origins = {
        f"http://127.0.0.1:{MCP_PORT}",
        f"http://localhost:{MCP_PORT}",
    } | _csv_env("CANVAS_MCP_ALLOWED_ORIGINS")
```

and the middleware is attached to the FastMCP Starlette app before uvicorn
starts serving:

```python
    app = mcp.streamable_http_app()
    app.add_middleware(_HTTPGuard, token=token, allowed_hosts=allowed_hosts,
                       allowed_origins=allowed_origins)
    uvicorn.run(app, host=MCP_HOST, port=MCP_PORT, log_level="warning")
```

Because it is middleware around the whole app, there is no route — present or
future — that can be reached without passing the guard.

**Why stdio skips all of this:**

```python
    if cli.stdio or os.environ.get("MCP_TRANSPORT", "").strip().lower() == "stdio":
        print("Canvas MCP server starting (stdio transport)", file=sys.stderr)
        mcp.run(transport="stdio")
        sys.exit(0)
```

Over stdio there is no network listener at all — the transport is a pipe that
exists only between the client and the child process it spawned. Nothing else
on the machine can write into it, so HTTP-style auth would protect nothing;
this is also the MCP spec's position (credentials for stdio servers come from
the environment, not from HTTP headers).

## 4. Client: `${VAR}` secret expansion in `app/mcp_manager.py`

The voice client's server registry (`mcp_servers.json`) must be shareable —
but the Canvas entry needs to send a secret header. The fix: the registry
stores a **placeholder**, and the real value is resolved from the process
environment (loaded from `.env`) only at the moment a connection is made.

```json
"headers": { "Authorization": "Bearer ${CANVAS_MCP_AUTH_TOKEN}" }
```

```python
_ENV_REF = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def _expand_env(value: str, missing: set[str]) -> str:
    """Replace ${VAR} references with values from the process environment.

    Secrets (bearer tokens, API keys) stay out of mcp_servers.json: the
    registry stores the placeholder and expansion happens only at connect
    time. Unset variable names are collected in `missing` so the connection
    can fail closed with a clear message instead of sending an empty secret.
    """
    def sub(match: re.Match) -> str:
        val = os.environ.get(match.group(1))
        if val is None:
            missing.add(match.group(1))
            return ""
        return val

    return _ENV_REF.sub(sub, value)
```

- The regex only matches the strict `${NAME}` form (letters, digits,
  underscore, not starting with a digit) — `$VAR`, `{x}`, and stray `$}` pass
  through literally, so ordinary header values can never be mangled.
- Missing variables are **collected, not defaulted**: substituting an empty
  string and carrying on would send `Authorization: Bearer ` — a request that
  *looks* authenticated in client logs but is guaranteed to 401 (or worse,
  silently succeed against a misconfigured server).

The connection task expands url, headers, and env together and refuses to
proceed if anything is unresolved:

```python
            missing: set[str] = set()
            url = _expand_env(cfg.url, missing)
            headers = {
                k: _expand_env(v, missing) for k, v in (cfg.headers or {}).items()
            }
            extra_env = {k: _expand_env(v, missing) for k, v in cfg.env.items()}
            if missing:
                raise RuntimeError(
                    "unset environment variable(s) referenced in server config: "
                    + ", ".join(sorted(missing))
                    + " — set them in .env and reconnect"
                )
```

Three properties worth noting:

- **Fail closed with a usable message.** The raised error lands in
  `MCPConnection.error` and is shown in Settings → MCP Servers, naming
  exactly which variable to set (`... referenced in server config:
  CANVAS_MCP_AUTH_TOKEN — set them in .env and reconnect`).
- **Expansion never touches the stored config.** `cfg` is read into *local*
  variables; `_persist()` later serializes the original `cfg` objects, so
  the placeholder — not the secret — is what gets written back to
  `mcp_servers.json`, no matter how many times servers are edited or
  reconnected from the UI.
- **It covers all three secret carriers**: `url` (some services put tokens in
  the URL), `headers` (the normal case), and `env` (secrets passed to
  stdio-spawned child servers).

## 5. The request lifecycle, end to end

```
.env (client)                    .env (server)
CANVAS_MCP_AUTH_TOKEN=abc...     CANVAS_MCP_AUTH_TOKEN=abc...
      │                                 │ read once at startup;
      ▼ expanded at connect time        ▼ refuses to start if absent
mcp_servers.json ──▶ MCPConnection ──▶ HTTP POST /mcp
"Bearer ${CANVAS_       (httpx)        Authorization: Bearer abc...
 MCP_AUTH_TOKEN}"                          │
                                           ▼
                                   ┌── _HTTPGuard ──────────────────┐
                                   │ 1. Host on allowlist?  ──403──▶│──▶ _audit http_denied
                                   │ 2. Origin absent/allowed? 403─▶│──▶ _audit http_denied
                                   │ 3. compare_digest(token)  401─▶│──▶ _audit http_denied
                                   └────────────┬───────────────────┘     (+ WWW-Authenticate)
                                                ▼ all pass
                                     FastMCP streamable HTTP app
                                     (tools / resources / prompts,
                                      every tool call audited)
```

## 6. Where each piece is verified

- `tests/test_mcp_security.py` (offline, part of `python -m unittest
  discover -s tests`):
  - `TestExpandEnv` — expansion of set variables, collection of missing ones,
    literal text passing through unchanged;
  - `TestConnectionFailsClosed` — a config referencing an unset variable must
    end **not connected** with the variable named in the error;
  - `TestRegistryKeepsPlaceholder` — `_persist()` must write the
    `${...}` placeholder, never the resolved secret.
- Live checks (also in `SECURITY_CHECKLIST.md`): start without
  `CANVAS_MCP_AUTH_TOKEN` → process exits 1; `curl` without / with a wrong
  token → 401; forged `Host` / `Origin` → 403; each rejection appears as an
  `http_denied` line in `examples/Canvas_MCP/logs/canvas-mcp-*.jsonl`.
