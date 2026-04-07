# mcp-salesforce

A **Model Context Protocol (MCP) server** that connects Claude (or any MCP-compatible AI client) to a live Salesforce org. It exposes Salesforce data as callable tools so an AI assistant can query accounts, contacts, opportunities, cases, and more in natural language.

**Part of a two-repo project:**
- **This repo** — the Salesforce MCP Server (data layer)
- [mcp-chat](https://github.com/rahul10cs/mcp-chat) — the Chat UI + Claude API backend (presentation layer)

---

## How It Fits Into the System

```
User types in browser
        │
        ▼
  [ mcp-chat ]  ← FastAPI app on port 3000
  app.py acts as MCP Client
        │  connects via SSE (HTTP + Server-Sent Events)
        ▼
  [ mcp-salesforce ]  ← THIS REPO — MCP Server on port 8000
  server.py exposes Salesforce as tools
        │  authenticates via OAuth Client Credentials Flow
        ▼
  [ Salesforce Org ]
  Returns live CRM data
```

The MCP server's only job is to expose Salesforce data as a set of named tools. It does not talk to Claude directly — it waits for an MCP client (like `mcp-chat`) to connect, list its tools, and call them.

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

### Step 1 — MCP Client connects (`mcp-chat` or Claude Code)
```
GET http://localhost:8000/sse
```
The client opens a persistent SSE connection. The server sends a session ID back. All further communication happens through this channel.

### Step 2 — Client lists available tools
```python
# mcp-chat/app.py calls:
tools_response = await session.list_tools()
```
The server responds with the full list of tools (names, descriptions, input schemas). This is how Claude knows what it can call.

### Step 3 — Claude decides to call a tool
Claude receives the user's message and the tool list. If the query needs Salesforce data (e.g. "show me accounts"), Claude responds with a `tool_use` block:
```json
{
  "type": "tool_use",
  "name": "get_accounts",
  "input": { "limit": 10 }
}
```

### Step 4 — MCP Client sends tool call to this server
```
POST http://localhost:8000/messages/?session_id=<id>
```
The client forwards the tool name and arguments to the MCP server.

### Step 5 — `call_tool()` runs in `server.py`
```python
@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[types.TextContent]:
    if name == "get_accounts":
        sf    = get_sf()          # authenticate to Salesforce
        limit = min(int(arguments.get("limit", 10)), 50)
        query = "SELECT Id, Name, Industry, Phone, Website, AnnualRevenue FROM Account"
        if arguments.get("name_filter"):
            query += f" WHERE Name LIKE '%{safe_str(arguments['name_filter'])}%'"
        query += f" LIMIT {limit}"
        result = sf.query(query)
        return [types.TextContent(type="text", text=records_to_text(result["records"]))]
```
The result is returned as plain text back through the SSE channel to the MCP client.

### Step 6 — Result flows back to Claude, then to the user
Claude receives the tool result, formats a natural-language response, and `mcp-chat` sends it back to the browser.

---

## Authentication — OAuth Client Credentials Flow

This server authenticates to Salesforce using **OAuth 2.0 Client Credentials Flow** (server-to-server, no user login required).

```python
# server.py — get_sf() function
def get_sf() -> Salesforce:
    resp = requests.post(
        f"{instance_url}/services/oauth2/token",
        data={
            "grant_type": "client_credentials",
            "client_id": client_id,
            "client_secret": client_secret,
        },
    )
    token = resp.json()
    return Salesforce(
        instance_url=token["instance_url"],
        session_id=token["access_token"],
    )
```

Every tool call triggers a fresh OAuth token request. The token is then passed directly to `simple_salesforce` as a session ID — no username or password needed.

**Salesforce setup required:**
1. Create a Connected App in Setup → App Manager
2. Enable OAuth → tick **Enable Client Credentials Flow**
3. Add scopes: `api`, `full`
4. Go to Manage → Edit Policies → set **Run As** user
5. Set **Permitted Users** to "All users may self-authorize"

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
