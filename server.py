#!/usr/bin/env python3
"""
MCP Server — Salesforce Org Tools
Gives Claude (or any MCP client) the ability to query your Salesforce org:
  • get_accounts       → list accounts with optional name filter
  • get_contacts       → list contacts with optional email filter
  • get_opportunities  → list opportunities with optional stage filter
  • get_cases          → list support cases with optional status filter
  • run_soql           → run any read-only SOQL query
  • get_org_info       → basic info about the connected Salesforce org
"""

import asyncio
import os
import re

import requests
import uvicorn
from dotenv import load_dotenv
from simple_salesforce import Salesforce, SalesforceAuthenticationFailed
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.routing import Mount, Route

from mcp.server import Server
from mcp.server.sse import SseServerTransport
from mcp.server.stdio import stdio_server
from mcp import types

load_dotenv()

server = Server("salesforce-mcp")


# ---------------------------------------------------------------------------
# Salesforce connection — reads credentials from environment variables
# ---------------------------------------------------------------------------

def get_sf() -> Salesforce:
    client_id     = os.getenv("SF_CLIENT_ID")
    client_secret = os.getenv("SF_CLIENT_SECRET")
    instance_url  = os.getenv("SF_INSTANCE_URL")

    if not all([client_id, client_secret, instance_url]):
        raise ValueError(
            "Missing Salesforce OAuth credentials. "
            "Set SF_CLIENT_ID, SF_CLIENT_SECRET, SF_INSTANCE_URL in your .env file."
        )

    resp = requests.post(
        f"{instance_url}/services/oauth2/token",
        data={
            "grant_type": "client_credentials",
            "client_id": client_id,
            "client_secret": client_secret,
        },
    )
    resp.raise_for_status()
    token = resp.json()

    return Salesforce(
        instance_url=token["instance_url"],
        session_id=token["access_token"],
    )


def safe_str(value: str) -> str:
    """Escape single quotes to prevent SOQL injection."""
    return value.replace("'", "\\'")


def records_to_text(records: list, empty_msg: str = "No records found.") -> str:
    if not records:
        return empty_msg
    lines = []
    for i, rec in enumerate(records, 1):
        clean = {k: v for k, v in rec.items() if k != "attributes"}
        lines.append(f"[{i}] " + " | ".join(f"{k}: {v}" for k, v in clean.items()))
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Tool definitions
# ---------------------------------------------------------------------------

@server.list_tools()
async def list_tools() -> list[types.Tool]:
    return [
        types.Tool(
            name="get_accounts",
            description=(
                "Fetch accounts from Salesforce. "
                "Optionally filter by account name (partial match). "
                "Returns Id, Name, Industry, Phone, Website, AnnualRevenue."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "name_filter": {
                        "type": "string",
                        "description": "Optional partial name to search for (e.g. 'Acme')",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max number of records to return (default 10, max 50)",
                        "minimum": 1,
                        "maximum": 50,
                    },
                },
                "required": [],
            },
        ),
        types.Tool(
            name="get_contacts",
            description=(
                "Fetch contacts from Salesforce. "
                "Optionally filter by email or last name. "
                "Returns Id, FirstName, LastName, Email, Phone, Account Name."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "email": {
                        "type": "string",
                        "description": "Optional email to filter by (exact match)",
                    },
                    "last_name": {
                        "type": "string",
                        "description": "Optional last name to filter by (partial match)",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max number of records to return (default 10, max 50)",
                        "minimum": 1,
                        "maximum": 50,
                    },
                },
                "required": [],
            },
        ),
        types.Tool(
            name="get_opportunities",
            description=(
                "Fetch opportunities from Salesforce. "
                "Optionally filter by stage. "
                "Returns Id, Name, StageName, Amount, CloseDate, Account Name."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "stage": {
                        "type": "string",
                        "description": "Optional stage filter e.g. 'Prospecting', 'Closed Won', 'Closed Lost'",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max number of records to return (default 10, max 50)",
                        "minimum": 1,
                        "maximum": 50,
                    },
                },
                "required": [],
            },
        ),
        types.Tool(
            name="get_cases",
            description=(
                "Fetch support cases from Salesforce. "
                "Optionally filter by status. "
                "Returns Id, CaseNumber, Subject, Status, Priority, Account Name."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "status": {
                        "type": "string",
                        "description": "Optional status filter e.g. 'New', 'Working', 'Closed'",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max number of records to return (default 10, max 50)",
                        "minimum": 1,
                        "maximum": 50,
                    },
                },
                "required": [],
            },
        ),
        types.Tool(
            name="run_soql",
            description=(
                "Run a custom read-only SOQL query against the Salesforce org. "
                "Only SELECT statements are allowed. No INSERT, UPDATE, DELETE. "
                "Example: SELECT Id, Name FROM Account WHERE Industry = 'Technology' LIMIT 5"
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "A valid SOQL SELECT query",
                    }
                },
                "required": ["query"],
            },
        ),
        types.Tool(
            name="get_org_info",
            description="Return basic information about the connected Salesforce org (name, id, org type).",
            inputSchema={"type": "object", "properties": {}, "required": []},
        ),
    ]


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------

@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[types.TextContent]:

    # ── get_accounts ─────────────────────────────────────────────────────────
    if name == "get_accounts":
        try:
            sf    = get_sf()
            limit = min(int(arguments.get("limit", 10)), 50)
            query = "SELECT Id, Name, Industry, Phone, Website, AnnualRevenue FROM Account"
            if arguments.get("name_filter"):
                query += f" WHERE Name LIKE '%{safe_str(arguments['name_filter'])}%'"
            query += f" LIMIT {limit}"
            result = sf.query(query)
            return [types.TextContent(type="text", text=records_to_text(result["records"]))]
        except SalesforceAuthenticationFailed:
            return [types.TextContent(type="text", text="Authentication failed. Check your Salesforce credentials.")]
        except Exception as e:
            return [types.TextContent(type="text", text=f"Error: {e}")]

    # ── get_contacts ─────────────────────────────────────────────────────────
    if name == "get_contacts":
        try:
            sf     = get_sf()
            limit  = min(int(arguments.get("limit", 10)), 50)
            query  = "SELECT Id, FirstName, LastName, Email, Phone, Account.Name FROM Contact"
            filters = []
            if arguments.get("email"):
                filters.append(f"Email = '{safe_str(arguments['email'])}'")
            if arguments.get("last_name"):
                filters.append(f"LastName LIKE '%{safe_str(arguments['last_name'])}%'")
            if filters:
                query += " WHERE " + " AND ".join(filters)
            query += f" LIMIT {limit}"
            result = sf.query(query)
            return [types.TextContent(type="text", text=records_to_text(result["records"]))]
        except SalesforceAuthenticationFailed:
            return [types.TextContent(type="text", text="Authentication failed. Check your Salesforce credentials.")]
        except Exception as e:
            return [types.TextContent(type="text", text=f"Error: {e}")]

    # ── get_opportunities ────────────────────────────────────────────────────
    if name == "get_opportunities":
        try:
            sf    = get_sf()
            limit = min(int(arguments.get("limit", 10)), 50)
            query = "SELECT Id, Name, StageName, Amount, CloseDate, Account.Name FROM Opportunity"
            if arguments.get("stage"):
                query += f" WHERE StageName = '{safe_str(arguments['stage'])}'"
            query += f" ORDER BY CloseDate DESC LIMIT {limit}"
            result = sf.query(query)
            return [types.TextContent(type="text", text=records_to_text(result["records"]))]
        except SalesforceAuthenticationFailed:
            return [types.TextContent(type="text", text="Authentication failed. Check your Salesforce credentials.")]
        except Exception as e:
            return [types.TextContent(type="text", text=f"Error: {e}")]

    # ── get_cases ────────────────────────────────────────────────────────────
    if name == "get_cases":
        try:
            sf    = get_sf()
            limit = min(int(arguments.get("limit", 10)), 50)
            query = "SELECT Id, CaseNumber, Subject, Status, Priority, Account.Name FROM Case"
            if arguments.get("status"):
                query += f" WHERE Status = '{safe_str(arguments['status'])}'"
            query += f" ORDER BY CreatedDate DESC LIMIT {limit}"
            result = sf.query(query)
            return [types.TextContent(type="text", text=records_to_text(result["records"]))]
        except SalesforceAuthenticationFailed:
            return [types.TextContent(type="text", text="Authentication failed. Check your Salesforce credentials.")]
        except Exception as e:
            return [types.TextContent(type="text", text=f"Error: {e}")]

    # ── run_soql ─────────────────────────────────────────────────────────────
    if name == "run_soql":
        try:
            query = arguments.get("query", "").strip()
            # Only allow SELECT statements
            if not re.match(r"^\s*SELECT\b", query, re.IGNORECASE):
                return [types.TextContent(type="text", text="Only SELECT queries are allowed.")]
            sf     = get_sf()
            result = sf.query(query)
            return [types.TextContent(type="text", text=records_to_text(result["records"]))]
        except SalesforceAuthenticationFailed:
            return [types.TextContent(type="text", text="Authentication failed. Check your Salesforce credentials.")]
        except Exception as e:
            return [types.TextContent(type="text", text=f"Error: {e}")]

    # ── get_org_info ─────────────────────────────────────────────────────────
    if name == "get_org_info":
        try:
            sf     = get_sf()
            result = sf.query("SELECT Id, Name, OrganizationType, IsSandbox FROM Organization LIMIT 1")
            if result["records"]:
                rec = result["records"][0]
                info = (
                    f"Org Name    : {rec.get('Name')}\n"
                    f"Org ID      : {rec.get('Id')}\n"
                    f"Org Type    : {rec.get('OrganizationType')}\n"
                    f"Is Sandbox  : {rec.get('IsSandbox')}"
                )
            else:
                info = "Could not retrieve org info."
            return [types.TextContent(type="text", text=info)]
        except SalesforceAuthenticationFailed:
            return [types.TextContent(type="text", text="Authentication failed. Check your Salesforce credentials.")]
        except Exception as e:
            return [types.TextContent(type="text", text=f"Error: {e}")]

    return [types.TextContent(type="text", text=f"Unknown tool: {name}")]


# ---------------------------------------------------------------------------
# Entry point — auto-selects transport based on TRANSPORT env var
#
#   TRANSPORT=stdio  (default) → for Claude Code local usage
#   TRANSPORT=sse              → for Render / remote / Claude Web usage
# ---------------------------------------------------------------------------

async def run_stdio() -> None:
    """Local mode — Claude Code connects via stdin/stdout."""
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream, write_stream, server.create_initialization_options()
        )


def run_sse() -> None:
    """Remote mode — any MCP client connects via HTTP + SSE."""
    sse_transport = SseServerTransport("/messages/")

    async def handle_sse(request: Request) -> None:
        async with sse_transport.connect_sse(
            request.scope, request.receive, request._send
        ) as streams:
            await server.run(
                streams[0], streams[1], server.create_initialization_options()
            )

    starlette_app = Starlette(
        routes=[
            Route("/sse", endpoint=handle_sse),
            Mount("/messages/", app=sse_transport.handle_post_message),
        ]
    )

    port = int(os.getenv("PORT", 8000))
    print(f"Salesforce MCP server running on http://0.0.0.0:{port}/sse")
    uvicorn.run(starlette_app, host="0.0.0.0", port=port)


if __name__ == "__main__":
    transport = os.getenv("TRANSPORT", "stdio")
    if transport == "sse":
        run_sse()
    else:
        asyncio.run(run_stdio())
