# mcp-salesforce

A **Model Context Protocol (MCP) server** that connects Claude (or any MCP-compatible AI client) to a live Salesforce org. It exposes Salesforce data as callable tools so an AI assistant can query accounts, contacts, opportunities, cases, and more in natural language.

**Part of a two-repo project:**
- **This repo** — the Salesforce MCP Server (data layer)
- [mcp-chat](https://github.com/rahul10cs/mcp-chat) — the Chat UI + Claude API backend (presentation layer)

---

## How It Fits Into the System

```
Browser (user)
      │
      │  1. Login with Salesforce (OAuth Authorization Code Flow)
      ▼
[ mcp-chat ]  ← FastAPI app on port 3000
  - handles SF OAuth, session management, Anthropic key
  - acts as MCP Client
      │
      │  2. GET /sse?sf_token=<user_token>&sf_instance=<org_url>
      ▼
[ mcp-salesforce ]  ← THIS REPO — MCP Server on port 8000
  - receives user's SF token via URL params
  - stores token per session
  - injects token into get_sf() via async context var
      │
      │  3. REST API calls with Bearer <sf_token>
      ▼
[ Salesforce Org ]
  Returns live CRM data
```

The MCP server's only job is to expose Salesforce data as named tools. It does not talk to Claude directly — it waits for an MCP client to connect, list tools, and call them. The user's Salesforce access token is passed in on connection so each user's data is isolated.

---

## What Is MCP?

**Model Context Protocol (MCP)** is an open standard that lets AI models call external tools in a structured way. Instead of hardcoding API integrations into a prompt, you register tools on an MCP server. Any MCP client (Claude Code, a custom app, etc.) can then discover and call those tools dynamically.

This server uses the **SSE transport** (Server-Sent Events over HTTP), which means any MCP client can connect to it over the network — locally or after deploying to the cloud.

---

## Project Structure

```
mcp-salesforce/
├── server.py          # The MCP server — all tools and Salesforce logic live here
├── requirements.txt   # Python dependencies
├── render.yaml        # Deployment config for Render.com
├── setup.sh           # One-time setup script (venv + Claude Code registration)
├── .env.example       # Template for environment variables
└── .gitignore
```

---

## Tools Exposed

| Tool | What It Does |
|---|---|
| `get_accounts` | List accounts, optionally filtered by name |
| `get_contacts` | List contacts, optionally filtered by email or last name |
| `get_opportunities` | List opportunities, optionally filtered by stage |
| `get_cases` | List support cases, optionally filtered by status |
| `run_soql` | Run any custom read-only SOQL SELECT query |
| `get_org_info` | Return org name, ID, type, and sandbox status |

---

## Step-by-Step: What Happens When a Tool Is Called

### Step 1 — MCP Client opens an SSE connection

```
GET http://localhost:8000/sse
```

The client (mcp-chat) makes an HTTP GET to `/sse`. The server keeps the connection open and immediately streams back a session endpoint via SSE:

```
event: endpoint
data: /messages/?session_id=f4f4738fc2b54cda98cc14faf55cef37
```

All further communication uses this session ID. The SSE connection stays alive for the duration of the request.

This is handled in `server.py` by the Starlette route:

```python
starlette_app = Starlette(
    routes=[
        Route("/sse", endpoint=handle_sse),               # client connects here
        Mount("/messages/", app=sse_transport.handle_post_message),  # client POSTs here
    ]
)
```

---

### Step 2 — MCP Handshake (`initialize`)

The client sends an `initialize` message to the session endpoint:

```
POST http://localhost:8000/messages/?session_id=<id>
Content-Type: application/json

{
  "jsonrpc": "2.0",
  "method": "initialize",
  "params": { "protocolVersion": "2024-11-05", "capabilities": {} }
}
```

The server responds (via SSE stream) with its capabilities. After this, the session is active.

---

### Step 3 — Client lists available tools

```
POST http://localhost:8000/messages/?session_id=<id>

{ "jsonrpc": "2.0", "method": "tools/list", "params": {} }
```

The server's `list_tools()` handler responds with all 6 tool definitions including their names, descriptions, and JSON schemas. The client (mcp-chat) converts these into the format Claude's API expects.

```python
@server.list_tools()
async def list_tools() -> list[types.Tool]:
    return [
        types.Tool(
            name="get_accounts",
            description="Fetch accounts from Salesforce...",
            inputSchema={ "type": "object", "properties": { "name_filter": ..., "limit": ... } }
        ),
        # ... 5 more tools
    ]
```

---

### Step 4 — Claude decides to call a tool

Claude receives the user message and tool list from mcp-chat. For a query like "show me my accounts", Claude responds to the Anthropic API with `stop_reason = "tool_use"`:

```json
{
  "type": "tool_use",
  "id": "toolu_01ABC...",
  "name": "get_accounts",
  "input": { "limit": 10 }
}
```

---

### Step 5 — MCP Client sends the tool call to this server

```
POST http://localhost:8000/messages/?session_id=<id>

{
  "jsonrpc": "2.0",
  "method": "tools/call",
  "params": {
    "name": "get_accounts",
    "arguments": { "limit": 10 }
  }
}
```

---

### Step 6 — `call_tool()` runs in `server.py`

The MCP SDK routes the call to the `call_tool` handler:

```python
@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[types.TextContent]:
    if name == "get_accounts":
        sf    = get_sf()          # ← OAuth token + simple_salesforce instance
        limit = min(int(arguments.get("limit", 10)), 50)
        query = "SELECT Id, Name, Industry, Phone, Website, AnnualRevenue FROM Account"
        if arguments.get("name_filter"):
            query += f" WHERE Name LIKE '%{safe_str(arguments['name_filter'])}%'"
        query += f" LIMIT {limit}"
        result = sf.query(query)  # ← Salesforce REST API call
        return [types.TextContent(type="text", text=records_to_text(result["records"]))]
```

`sf.query()` makes a `GET` request to Salesforce's REST API:

```
GET https://orgfarm-xxx.my.salesforce.com/services/data/v59.0/query
    ?q=SELECT+Id,Name,Industry...+FROM+Account+LIMIT+10
Authorization: Bearer 00DgK00000MqZJZ!...
```

Salesforce returns a JSON payload with a `records` array. `records_to_text()` formats it as a readable numbered list:

```python
def records_to_text(records: list, empty_msg: str = "No records found.") -> str:
    if not records:
        return empty_msg
    lines = []
    for i, rec in enumerate(records, 1):
        clean = {k: v for k, v in rec.items() if k != "attributes"}  # strip SF metadata
        lines.append(f"[{i}] " + " | ".join(f"{k}: {v}" for k, v in clean.items()))
    return "\n".join(lines)
```

Output looks like:
```
[1] Id: 001... | Name: Edge Communications | Industry: Electronics | Phone: (512) 757-6000
[2] Id: 001... | Name: Burlington Textiles | Industry: Apparel | Phone: (336) 222-7000
...
```

This plain text is what gets sent back to Claude as the tool result.

---

### Step 7 — Result flows back to Claude, then to the user

The tool result travels back through the SSE stream to mcp-chat, which appends it to the Claude conversation. Claude receives the Salesforce data and generates a formatted natural-language response, which mcp-chat returns to the browser.

---

## Authentication — Dual Mode

`get_sf()` supports two auth modes depending on how the MCP connection was opened:

```python
def get_sf() -> Salesforce:
    # Mode 1: per-user token injected via async context (from mcp-chat OAuth login)
    creds = _current_sf_creds.get()
    if creds and creds.get("token"):
        return Salesforce(
            instance_url=creds["instance_url"],
            session_id=creds["token"],   # user's SF access token
        )

    # Mode 2: Client Credentials fallback (env vars — for CLI / direct use)
    resp = requests.post(
        f"{instance_url}/services/oauth2/token",
        data={
            "grant_type":    "client_credentials",
            "client_id":     client_id,
            "client_secret": client_secret,
        },
    )
    token = resp.json()
    return Salesforce(instance_url=token["instance_url"], session_id=token["access_token"])
```

| Mode | When used | How |
|---|---|---|
| Per-user token | Connecting via mcp-chat browser UI | User's SF token passed via `?sf_token=` on SSE URL |
| Client Credentials | CLI / direct MCP client / testing | `SF_CLIENT_ID` + `SF_CLIENT_SECRET` in `.env` |

---

## Per-Session Token Injection

When mcp-chat connects to this server, it appends the user's SF token to the SSE URL:

```
GET http://localhost:8000/sse?sf_token=00DgK...&sf_instance=https://orgfarm-xxx.my.salesforce.com
```

The server captures this token and stores it against the MCP session ID so `get_sf()` can use it when tools are called:

### Step A — Capture session_id from SSE endpoint event

The SSE transport assigns a `session_id` and sends it to the client in the first SSE event:
```
event: endpoint
data: /messages/?session_id=f4f4738fc2b54cda98cc14faf55cef37
```

`handle_sse()` intercepts this event to capture the session_id and store the token:

```python
async def handle_sse(request: Request) -> None:
    sf_token    = request.query_params.get("sf_token")
    sf_instance = request.query_params.get("sf_instance")
    session_id_holder = {}

    async def capturing_send(message):
        # Intercept the endpoint SSE event to get the session_id
        if message["type"] == "http.response.body" and "id" not in session_id_holder:
            body = message.get("body", b"").decode("utf-8", errors="ignore")
            m = re.search(r"session_id=([^\s\"&\n]+)", body)
            if m and sf_token:
                sid = m.group(1)
                session_id_holder["id"] = sid
                _session_creds[sid] = {"token": sf_token, "instance_url": sf_instance}
        await original_send(message)

    async with sse_transport.connect_sse(request.scope, request.receive, capturing_send) as streams:
        await server.run(streams[0], streams[1], ...)
    # cleanup: _session_creds.pop(sid) when connection closes
```

### Step B — Inject token into async context on each tool call POST

Tool calls arrive as `POST /messages/?session_id=<id>`. A wrapper around `handle_post_message` reads the session_id from the URL, looks up the token, and injects it into the async context before routing the request:

```python
async def handle_post_with_creds(scope, receive, send):
    qs         = parse_qs(scope.get("query_string", b"").decode())
    session_id = (qs.get("session_id") or [None])[0]
    creds      = _session_creds.get(session_id)   # look up stored token

    token = _current_sf_creds.set(creds)          # inject into async context
    try:
        await sse_transport.handle_post_message(scope, receive, send)
    finally:
        _current_sf_creds.reset(token)            # restore after request

starlette_app = Starlette(
    routes=[
        Route("/sse", endpoint=handle_sse),
        Mount("/messages/", app=handle_post_with_creds),   # ← uses wrapper
    ]
)
```

When `call_tool()` then calls `get_sf()`, the context var already holds the right token for that user's session.

**Salesforce Connected App setup required:**
1. **Setup → App Manager → New Connected App**
2. Enable OAuth → tick **Authorization Code and Credentials Flow** (for browser login)
3. Also tick **Enable Client Credentials Flow** (for CLI/direct use fallback)
4. Add Callback URL: `http://localhost:3000/auth/salesforce/callback`
5. Add scopes: `api`, `refresh_token`, `full`
6. Uncheck **Require PKCE**
7. Manage → Edit Policies → set **Run As** user (for Client Credentials fallback)
8. Set **Permitted Users** to "All users may self-authorize"

---

## SOQL Injection Protection

The `safe_str()` helper escapes single quotes in all user-supplied filter values before they are interpolated into SOQL:

```python
def safe_str(value: str) -> str:
    return value.replace("'", "\\'")
```

The `run_soql` tool also validates that only `SELECT` statements are allowed:

```python
if not re.match(r"^\s*SELECT\b", query, re.IGNORECASE):
    return [types.TextContent(type="text", text="Only SELECT queries are allowed.")]
```

---

## Transport Modes

Controlled by the `TRANSPORT` environment variable in `.env`:

| Value | When to use |
|---|---|
| `stdio` | Local use with Claude Code (default) |
| `sse` | Running as an HTTP server (local or deployed) |

```python
# server.py — entry point
if __name__ == "__main__":
    transport = os.getenv("TRANSPORT", "stdio")
    if transport == "sse":
        run_sse()   # starts uvicorn HTTP server on PORT (default 8000)
    else:
        asyncio.run(run_stdio())   # communicates via stdin/stdout
```

---

## Local Setup

### Prerequisites
- Python 3.10+
- A Salesforce org with a Connected App configured (see Authentication above)

### Install & Run

```bash
# Clone the repo
git clone https://github.com/rahul10cs/mcp-salesforce.git
cd mcp-salesforce

# Create virtual environment
python3 -m venv .venv
source .venv/bin/activate

# Install dependencies
pip install -r requirements.txt

# Copy and fill in credentials
cp .env.example .env
# Edit .env with your SF_CLIENT_ID, SF_CLIENT_SECRET, SF_INSTANCE_URL

# Start the server (SSE mode for use with mcp-chat)
TRANSPORT=sse python server.py
```

Server starts at `http://localhost:8000/sse`.

---

## Environment Variables

| Variable | Description |
|---|---|
| `SF_CLIENT_ID` | Connected App Consumer Key |
| `SF_CLIENT_SECRET` | Connected App Consumer Secret |
| `SF_INSTANCE_URL` | Your org URL e.g. `https://myorg.my.salesforce.com` |
| `TRANSPORT` | `sse` for HTTP mode, `stdio` for Claude Code local mode |
| `PORT` | HTTP port (default `8000`) |

---

## Deploying to Render

A `render.yaml` is included. Push this repo to GitHub, connect it to [Render.com](https://render.com), and set the environment variables in the Render dashboard. The `startCommand` is `python server.py` with `TRANSPORT=sse`.

After deploying, copy the Render URL (e.g. `https://salesforce-mcp.onrender.com/sse`) and set it as `MCP_SERVER_URL` in the [mcp-chat](https://github.com/rahul10cs/mcp-chat) deployment.

---

## Dependencies

```
mcp>=1.0.0              # Model Context Protocol SDK
simple-salesforce>=1.12.0  # Salesforce REST API client
python-dotenv>=1.0.0    # Loads .env files
starlette>=0.27.0       # ASGI framework (used for SSE transport routing)
uvicorn>=0.24.0         # ASGI server
requests                # HTTP client for OAuth token exchange
```
