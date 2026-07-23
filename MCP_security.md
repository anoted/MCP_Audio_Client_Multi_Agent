# MCP security: what this project does, what the standard requires, and the MCP 2.0 path

This project speaks **MCP 1.x** (the protocol line whose released revisions are
2024-11-05, 2025-03-26, 2025-06-18, and 2025-11-25). This document covers three
things:

1. the transport security **implemented here** (bearer-token streamable HTTP for
   the Canvas server, hardened per current standard practice),
2. what **full standard practice** looks like under MCP 1.x — proper OAuth 2.1
   for user authorization — and how to upgrade this deployment to it, and
3. how the security posture should **improve when MCP 2.0 arrives**.

Companion docs: `MCP_security_code_walkthrough.md` (the raw code behind each
mechanism, explained), `SECURITY_CHECKLIST.md` (manual verification steps),
and `FAILURE_MODES.md` (failure catalog). Workflow-layer defenses (human approval
gates, reviewer/verifier passes, pseudonymization, injection guard, dual audit
logs) are described in `README.md` and are unchanged by this document — they
sit *above* the transport and remain the primary safeguards.

## 1. Transport security as implemented

| Server | Transport | Authentication | Notes |
|---|---|---|---|
| `demo` | stdio | none needed | private pipe owned by the spawning client |
| `Canvas` | **streamable HTTP** (run separately) | **static bearer token** | hardened endpoint, details below |
| `deepwiki`, `microsoft_learn`, `huggingface` | HTTPS (remote) | anonymous public endpoints | read-only docs services |
| `cloudflare_docs`, `gitmcp_docs` | HTTPS + SSE (remote) | anonymous public endpoints | SSE is the deprecated 2024-11-05 transport; fine as a client, don't build new servers on it |

The Canvas MCP server (`examples/Canvas_MCP/canvas_mcp_server.py`) holds a
**Canvas API token with full account power**, so its HTTP endpoint is treated
as a protected resource:

- **Bearer-token auth** — every request must carry
  `Authorization: Bearer <CANVAS_MCP_AUTH_TOKEN>`. Missing/invalid tokens get
  `401` with a `WWW-Authenticate: Bearer` challenge, as the MCP auth spec
  requires of a resource server. Comparison uses `secrets.compare_digest`
  (constant-time, no timing oracle). The server **refuses to start** over HTTP
  without a token (`CANVAS_MCP_ALLOW_NO_AUTH=1` is a sandbox-only escape
  hatch).
- **High-entropy token, generated not invented** —
  `python canvas_mcp_server.py --make-token` prints a 256-bit urlsafe token.
- **Secrets never live in config files that describe the connection** — the
  client's `mcp_servers.json` stores `"Authorization": "Bearer
  ${CANVAS_MCP_AUTH_TOKEN}"`; `app/mcp_manager.py` expands `${VAR}` references
  from the environment (`.env`) at connect time only, and **fails closed** with
  a clear error if the variable is unset rather than sending an empty secret.
  The registry JSON stays committable/shareable.
- **DNS-rebinding defense** — `Host` and `Origin` headers are validated
  against a localhost allowlist (extendable via `CANVAS_MCP_ALLOWED_HOSTS` /
  `CANVAS_MCP_ALLOWED_ORIGINS`); anything else is rejected with `403`. This is
  the MCP spec's explicitly recommended protection for locally running HTTP
  servers.
- **Localhost bind by default** (`MCP_HOST=127.0.0.1`). Binding elsewhere
  prints a loud warning: bearer tokens over plain HTTP are readable in
  transit, so any non-local exposure must go behind an HTTPS reverse proxy.
- **Auth failures are audited** — rejected requests (bad token / Host /
  Origin) are appended to the server's own JSONL audit log alongside tool
  calls, so probing shows up on the server side of the trust boundary.
- **Tokens only in the `Authorization` header** — never in URLs or query
  strings (they end up in logs and browser history), on either side.
- **stdio stays auth-free by design** — per spec, stdio transports do not use
  HTTP auth; credentials come from the environment and the pipe belongs to
  the client that spawned the process. `--stdio` / `MCP_TRANSPORT=stdio`
  preserves this mode.

### Why a static bearer token is acceptable here — and its limits

MCP 1.x makes authorization **optional** and OAuth 2.1 the standard *when the
resource needs real user auth*. For a **single-user, localhost, one-machine**
deployment — this project's default — a pre-shared high-entropy bearer token
is accepted current practice: there is exactly one user, one client, and one
resource, so an authorization server would add moving parts without adding a
distinct principal to authorize. The limits are real, though:

- one token = one scope = everything the Canvas API token can do (no
  read-only grant, no per-tool scoping at the transport level — that scoping
  currently happens in the *client's* workflow layer),
- no expiry or rotation story beyond "generate a new one",
- no user identity in the token, so the server-side audit log can't say *who*
  called (it's always "the holder of the token").

The moment a second user, a second machine, or a public hostname enters the
picture, upgrade to the OAuth profile below.

## 2. Standard practice under MCP 1.x: proper OAuth 2.1

This is what the MCP authorization spec (2025-06-18 revision, carried forward
by 2025-11-25) actually prescribes for HTTP transports, and what a
production deployment of this project should implement. Roles:

- **MCP server** → OAuth 2.0 **resource server** (RS)
- **MCP client** → OAuth **public client** using **authorization code + PKCE**
- **Authorization server** (AS) → an IdP you already run (Keycloak, Auth0,
  Microsoft Entra ID, Okta...) — the MCP server does *not* have to be its own AS

The required pieces, with the RFCs the spec cites:

| Requirement | Mechanism |
|---|---|
| Server advertises how to get authorized | **RFC 9728** Protected Resource Metadata at `/.well-known/oauth-protected-resource`, and `401` responses carry `WWW-Authenticate` pointing at it |
| Client discovers the AS | **RFC 8414** Authorization Server Metadata |
| Client obtains tokens | **OAuth 2.1** authorization code flow with **PKCE (S256)** — mandatory for public clients; no implicit flow, no password grant |
| Client identifies itself without pre-registration | **RFC 7591** Dynamic Client Registration; the 2025-11-25 revision adds **Client ID Metadata Documents** (an HTTPS URL *is* the client id) as the preferred, phishing-resistant alternative |
| Tokens are bound to *this* server | **RFC 8707** Resource Indicators — the client sends `resource=<MCP server URL>`; the AS mints the token with that audience; the server **MUST validate the audience** and reject tokens minted for anything else |
| Tokens travel safely | `Authorization: Bearer` header only; HTTPS mandatory for non-localhost; short-lived access tokens + refresh-token rotation |
| No confused deputy | The MCP server **MUST NOT pass the client's token upstream** (e.g., to the Canvas REST API). Upstream calls use the server's *own* credential — exactly what this project already does: the Canvas API token stays server-side in `examples/Canvas_MCP/.env` and is never visible to the MCP client or the LLM |
| Sessions are not auth | `Mcp-Session-Id` must be non-deterministic and never used as proof of identity — verify the bearer token on every request |

### Concrete upgrade path for this repo

1. Stand up an IdP realm; register the Canvas MCP server as a resource
   (`resource=https://mcp.yourhost/mcp`), define scopes that mirror the
   client's existing read/modify tool classification — e.g. `canvas.read`,
   `canvas.write`, `canvas.grade`, `canvas.announce`.
2. Replace the static-token check in `_HTTPGuard` with a **TokenVerifier**:
   validate the JWT signature against the IdP's JWKS (or use token
   introspection), check `iss`, `aud` (RFC 8707 audience), `exp`, and scopes;
   the MCP Python SDK ships resource-server support
   (`mcp.server.auth`) for exactly this.
3. Serve the RFC 9728 metadata document and put the resource-metadata URL in
   the `WWW-Authenticate` challenge.
4. Enforce scopes **per tool** on the server: `grade_submission` requires
   `canvas.grade`, `create_announcement` requires `canvas.announce`,
   `list_*`/`get_*`/`render_*` require only `canvas.read`. Today that
   least-privilege split is enforced by the *client's* workflow layer
   (initiator classification + approval gates); OAuth scopes move it to the
   server side of the trust boundary, where a compromised or third-party
   client can't skip it.
5. TLS everywhere non-local (reverse proxy with HTTPS in front of uvicorn),
   short token lifetimes (minutes, not days), refresh rotation, and
   revocation via the IdP.
6. Keep the workflow-layer controls: OAuth authenticates and authorizes the
   *client*; it does nothing about prompt injection, over-eager agents, or a
   wrong grade — the human approval gates stay.

## 3. MCP 2.0: how this security should improve

MCP 2.0 is the announced next major revision; details below follow the public
roadmap and working-group discussions and **may change before release** —
treat this section as a planning direction, not a spec citation.

Expected directions, and what each one means for this project:

- **Fine-grained, standardized authorization.** Per-tool / per-resource
  scopes and consent become protocol-level concepts instead of each server
  inventing its own. *Here:* the initiator's read/modify classification and
  the skill-based server routing map almost one-to-one onto declarative tool
  scopes — plan to publish that classification from the server (tool
  annotations) rather than inferring it client-side with an LLM, and enforce
  it server-side.
- **Enterprise identity brokering / SSO.** First-class patterns for an IdP in
  front of every MCP server, Client ID Metadata Documents replacing ad-hoc
  dynamic registration, and cross-app identity so one login covers a fleet of
  servers. *Here:* the instructor logs in once (university SSO); the Canvas
  MCP server sees an identity, not just "the token holder" — the server-side
  audit log finally gets a *who*.
- **Delegation chains for multi-agent systems.** Token exchange (RFC 8693
  style) / on-behalf-of flows so a manager agent can hand a **downscoped**
  credential to each worker instead of every sub-agent sharing one omnipotent
  connection. *Here:* this is the biggest win — the manager→planner→worker→
  reviewer/verifier pipeline currently restricts sub-agent tool surfaces
  inside the client process; with delegation, a worker executing "grade step
  3" would hold a token that *cryptographically* can only grade that
  assignment, and the reviewer/verifier would hold read-only tokens.
- **Server identity, registries, and integrity.** Signed server manifests and
  registry provenance to counter tool poisoning, rug-pull updates, and
  typosquatted servers. *Here:* pin and verify the servers in
  `mcp_servers.json` by signature instead of by URL; alert when a server's
  tool list or descriptions change between sessions (today the injection
  guard only screens tool *results*, not drifting tool *definitions*).
- **Sender-constrained tokens.** DPoP / mTLS-style binding so a stolen bearer
  token is useless without the client's key — retiring the pure-bearer model
  this project (and most of the ecosystem) uses today.
- **Asynchronous / long-running operations** (piloted as "tasks" in the
  2025-11-25 revision) with defined re-authorization semantics, so a
  long grading run can't outlive the consent that started it. *Here:* map
  approval-gate decisions to the task lifecycle instead of the socket
  lifetime.

### Migration checklist (do now → ready for 2.0)

- [x] Network transport requires auth; secrets in env, not config; fail-closed
      expansion; rebinding defenses; audited denials (this change).
- [ ] Move to IdP-issued short-lived JWTs + RFC 9728 metadata + audience
      validation (section 2) — everything in 2.0 builds on this baseline.
- [ ] Define the scope vocabulary now (`canvas.read` / `canvas.write` /
      `canvas.grade` / `canvas.announce`) and start enforcing it server-side.
- [ ] Emit the caller identity into the Canvas server's audit log once tokens
      carry one.
- [ ] Watch the 2.0 drafts for delegation/token-exchange and adopt per-worker
      downscoped tokens in `app/agents.py` grant routing when available.
- [ ] Snapshot + diff server tool definitions between sessions until signed
      manifests exist.

## 4. Threat model summary

| Threat | Mitigation (today) |
|---|---|
| Anything on localhost calling the Canvas endpoint | bearer token required; constant-time compare; denials audited |
| Malicious webpage in the instructor's browser reaching `127.0.0.1:8017` (DNS rebinding / CSRF-style) | Host + Origin allowlists → 403; token unknown to the page |
| Token leakage via config sharing / VCS | registry stores `${CANVAS_MCP_AUTH_TOKEN}` placeholder; real value only in gitignored `.env` files |
| Token leakage via URLs/logs | header-only transmission; both audit logs PII-mask and never record the Authorization header |
| Confused deputy (client's credential reaching Canvas) | never happens by construction — the Canvas API token is the server's own credential and never crosses the MCP boundary |
| Prompt injection in course content / submissions | client-side injection guard + human approval gates + reviewer/verifier (unchanged) |
| Over-privileged agent actions | client-side read/modify grant routing + approval checkpoints; server-side scopes are the OAuth upgrade (section 2) |
| Network eavesdropping | localhost-only by default; HTTPS reverse proxy required before any remote exposure |
