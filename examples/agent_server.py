"""
AnaModel Agent Server
OpenAI-compatible FastAPI endpoint with:
  - Built-in tools: web search, weather, news, crypto, calculator, Python
  - User integrations: GitHub, Stripe, Supabase, Slack, Notion, Linear, Resend, Twilio, custom HTTP API
  - Automatic credential detection from conversation (users just paste tokens in chat)
  - Chat UI served at /chat
  - Streaming (SSE)

Two ways to connect services:

1. Paste token/key in chat (no setup needed):
   "My GitHub token is ghp_xxx, show me my repos"
   "Stripe key: sk_live_xxx — any failed payments today?"
   "supabase url: https://xxx.supabase.co key: eyJ... — how many users signed up?"

2. Pass credentials in the request under `integrations`:
   {
     "model": "AnaModel",
     "messages": [...],
     "integrations": {
       "github":   {"token": "ghp_xxx"},
       "stripe":   {"api_key": "sk_xxx"},
       "supabase": {"url": "https://xxx.supabase.co", "anon_key": "eyJ..."},
       "slack":    {"token": "xoxb-xxx"},
       "http_api": {"base_url": "https://api.example.com", "auth_header": "Bearer tok"}
     }
   }
"""

import json, re, math, io, datetime, contextlib
import os, time, uuid, urllib.request, urllib.error, urllib.parse
from typing import Optional, Dict, Any
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException, Header, Request
from fastapi.responses import StreamingResponse, HTMLResponse
from pydantic import BaseModel
from openai import OpenAI

# ── Config ────────────────────────────────────────────────────────────────────

LITELLM_BASE_URL = os.environ.get("LITELLM_BASE_URL", "http://136.65.123.94/v1")
LITELLM_API_KEY  = os.environ["LITELLM_API_KEY"]
DEFAULT_MODEL    = "AnaModel"
MAX_STEPS        = 15
TAVILY_API_KEY   = os.environ.get("TAVILY_API_KEY", "")

client = OpenAI(base_url=LITELLM_BASE_URL, api_key=LITELLM_API_KEY)

# ── Base tools (always available) ─────────────────────────────────────────────

BASE_TOOLS = [
    {"type": "function", "function": {
        "name": "web_search",
        "description": "Search the web for current information — news, facts, events, people, companies. Use for anything that needs up-to-date information.",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string"},
            "num_results": {"type": "integer", "description": "default 6"},
        }, "required": ["query"]},
    }},
    {"type": "function", "function": {
        "name": "get_news",
        "description": "Get latest news headlines by topic or country.",
        "parameters": {"type": "object", "properties": {
            "topic": {"type": "string", "description": "e.g. 'Nigeria', 'technology', 'sports'. Empty = top headlines."},
        }, "required": []},
    }},
    {"type": "function", "function": {
        "name": "get_weather",
        "description": "Get current weather and today's forecast for any city.",
        "parameters": {"type": "object", "properties": {
            "location": {"type": "string"},
        }, "required": ["location"]},
    }},
    {"type": "function", "function": {
        "name": "get_crypto_price",
        "description": "Get live cryptocurrency prices.",
        "parameters": {"type": "object", "properties": {
            "coins": {"type": "string", "description": "e.g. 'bitcoin,ethereum,solana'"},
        }, "required": ["coins"]},
    }},
    {"type": "function", "function": {
        "name": "http_get",
        "description": "Fetch any public URL — articles, docs, APIs.",
        "parameters": {"type": "object", "properties": {
            "url": {"type": "string"},
        }, "required": ["url"]},
    }},
    {"type": "function", "function": {
        "name": "calculator",
        "description": "Evaluate a math expression.",
        "parameters": {"type": "object", "properties": {
            "expression": {"type": "string"},
        }, "required": ["expression"]},
    }},
    {"type": "function", "function": {
        "name": "run_python",
        "description": "Execute a Python snippet. Returns stdout. Use for data processing, analysis, or computation.",
        "parameters": {"type": "object", "properties": {
            "code": {"type": "string"},
        }, "required": ["code"]},
    }},
    {"type": "function", "function": {
        "name": "get_time",
        "description": "Returns current UTC date and time.",
        "parameters": {"type": "object", "properties": {}},
    }},
]

# ── Integration tool definitions ───────────────────────────────────────────────

GITHUB_TOOLS = [
    {"type": "function", "function": {
        "name": "github_list_repos",
        "description": "List the user's GitHub repositories.",
        "parameters": {"type": "object", "properties": {
            "per_page": {"type": "integer", "description": "default 10"},
        }, "required": []},
    }},
    {"type": "function", "function": {
        "name": "github_list_issues",
        "description": "List issues in a GitHub repository.",
        "parameters": {"type": "object", "properties": {
            "repo": {"type": "string", "description": "owner/repo format, e.g. 'octocat/Hello-World'"},
            "state": {"type": "string", "description": "'open' or 'closed', default 'open'"},
        }, "required": ["repo"]},
    }},
    {"type": "function", "function": {
        "name": "github_create_issue",
        "description": "Create a new issue in a GitHub repository.",
        "parameters": {"type": "object", "properties": {
            "repo":  {"type": "string", "description": "owner/repo"},
            "title": {"type": "string"},
            "body":  {"type": "string"},
        }, "required": ["repo", "title"]},
    }},
    {"type": "function", "function": {
        "name": "github_read_file",
        "description": "Read a file from a GitHub repository.",
        "parameters": {"type": "object", "properties": {
            "repo":   {"type": "string", "description": "owner/repo"},
            "path":   {"type": "string", "description": "file path, e.g. 'README.md'"},
            "branch": {"type": "string", "description": "default 'main'"},
        }, "required": ["repo", "path"]},
    }},
    {"type": "function", "function": {
        "name": "github_search_code",
        "description": "Search code across GitHub repositories.",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string", "description": "search query, e.g. 'function login repo:myorg/myapp'"},
        }, "required": ["query"]},
    }},
    {"type": "function", "function": {
        "name": "github_create_pr",
        "description": "Create a pull request.",
        "parameters": {"type": "object", "properties": {
            "repo":   {"type": "string"},
            "title":  {"type": "string"},
            "body":   {"type": "string"},
            "head":   {"type": "string", "description": "source branch"},
            "base":   {"type": "string", "description": "target branch, default 'main'"},
        }, "required": ["repo", "title", "head"]},
    }},
]

STRIPE_TOOLS = [
    {"type": "function", "function": {
        "name": "stripe_get_balance",
        "description": "Get the Stripe account balance.",
        "parameters": {"type": "object", "properties": {}},
    }},
    {"type": "function", "function": {
        "name": "stripe_list_payments",
        "description": "List recent Stripe payment intents.",
        "parameters": {"type": "object", "properties": {
            "limit": {"type": "integer", "description": "default 10"},
        }, "required": []},
    }},
    {"type": "function", "function": {
        "name": "stripe_get_customer",
        "description": "Look up a Stripe customer by email or ID.",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string", "description": "email address or customer ID"},
        }, "required": ["query"]},
    }},
    {"type": "function", "function": {
        "name": "stripe_create_refund",
        "description": "Issue a refund for a Stripe payment intent.",
        "parameters": {"type": "object", "properties": {
            "payment_intent_id": {"type": "string"},
            "amount": {"type": "integer", "description": "Amount in smallest currency unit (e.g. cents). Omit for full refund."},
        }, "required": ["payment_intent_id"]},
    }},
]

SUPABASE_TOOLS = [
    {"type": "function", "function": {
        "name": "supabase_list_tables",
        "description": "List all tables in the Supabase database.",
        "parameters": {"type": "object", "properties": {}},
    }},
    {"type": "function", "function": {
        "name": "supabase_query",
        "description": "Query a Supabase table. Returns matching rows.",
        "parameters": {"type": "object", "properties": {
            "table":   {"type": "string"},
            "select":  {"type": "string", "description": "columns to return, default '*'"},
            "filter":  {"type": "string", "description": "filter as key=value, e.g. 'status=active'"},
            "limit":   {"type": "integer", "description": "default 20"},
        }, "required": ["table"]},
    }},
    {"type": "function", "function": {
        "name": "supabase_insert",
        "description": "Insert a new row into a Supabase table.",
        "parameters": {"type": "object", "properties": {
            "table": {"type": "string"},
            "data":  {"type": "object", "description": "key-value pairs for the row"},
        }, "required": ["table", "data"]},
    }},
    {"type": "function", "function": {
        "name": "supabase_update",
        "description": "Update rows in a Supabase table.",
        "parameters": {"type": "object", "properties": {
            "table":  {"type": "string"},
            "data":   {"type": "object", "description": "fields to update"},
            "filter": {"type": "string", "description": "filter as key=value, e.g. 'id=123'"},
        }, "required": ["table", "data", "filter"]},
    }},
]

SLACK_TOOLS = [
    {"type": "function", "function": {
        "name": "slack_list_channels",
        "description": "List Slack channels in the workspace.",
        "parameters": {"type": "object", "properties": {}},
    }},
    {"type": "function", "function": {
        "name": "slack_send_message",
        "description": "Send a message to a Slack channel.",
        "parameters": {"type": "object", "properties": {
            "channel": {"type": "string", "description": "channel name or ID, e.g. '#general'"},
            "text":    {"type": "string"},
        }, "required": ["channel", "text"]},
    }},
    {"type": "function", "function": {
        "name": "slack_get_messages",
        "description": "Get recent messages from a Slack channel.",
        "parameters": {"type": "object", "properties": {
            "channel": {"type": "string"},
            "limit":   {"type": "integer", "description": "default 10"},
        }, "required": ["channel"]},
    }},
]

HTTP_API_TOOLS = [
    {"type": "function", "function": {
        "name": "api_call",
        "description": "Call the user's connected API. Use this to interact with any custom service the user has connected.",
        "parameters": {"type": "object", "properties": {
            "method":  {"type": "string", "description": "HTTP method: GET, POST, PUT, DELETE, PATCH"},
            "path":    {"type": "string", "description": "API path, e.g. '/users/123'"},
            "body":    {"type": "object", "description": "request body for POST/PUT/PATCH"},
            "headers": {"type": "object", "description": "extra headers"},
        }, "required": ["method", "path"]},
    }},
]

NOTION_TOOLS = [
    {"type": "function", "function": {
        "name": "notion_search",
        "description": "Search pages and databases in the user's Notion workspace.",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string"},
        }, "required": ["query"]},
    }},
    {"type": "function", "function": {
        "name": "notion_create_page",
        "description": "Create a new page in a Notion database or as a child of a parent page.",
        "parameters": {"type": "object", "properties": {
            "parent_id": {"type": "string", "description": "database ID or parent page ID"},
            "title":     {"type": "string"},
            "content":   {"type": "string", "description": "markdown-style content for the page body"},
        }, "required": ["parent_id", "title"]},
    }},
    {"type": "function", "function": {
        "name": "notion_get_page",
        "description": "Read the content of a Notion page by ID.",
        "parameters": {"type": "object", "properties": {
            "page_id": {"type": "string"},
        }, "required": ["page_id"]},
    }},
]

LINEAR_TOOLS = [
    {"type": "function", "function": {
        "name": "linear_list_issues",
        "description": "List issues in the Linear workspace, optionally filtered by state or assignee.",
        "parameters": {"type": "object", "properties": {
            "state":    {"type": "string", "description": "e.g. 'Todo', 'In Progress', 'Done'"},
            "limit":    {"type": "integer", "description": "default 10"},
        }, "required": []},
    }},
    {"type": "function", "function": {
        "name": "linear_create_issue",
        "description": "Create a new issue in Linear.",
        "parameters": {"type": "object", "properties": {
            "title":       {"type": "string"},
            "description": {"type": "string"},
            "priority":    {"type": "integer", "description": "0=none, 1=urgent, 2=high, 3=medium, 4=low"},
        }, "required": ["title"]},
    }},
    {"type": "function", "function": {
        "name": "linear_list_projects",
        "description": "List projects in the Linear workspace.",
        "parameters": {"type": "object", "properties": {}},
    }},
]

RESEND_TOOLS = [
    {"type": "function", "function": {
        "name": "send_email",
        "description": "Send an email via Resend on behalf of the user.",
        "parameters": {"type": "object", "properties": {
            "to":      {"type": "string", "description": "recipient email address"},
            "subject": {"type": "string"},
            "body":    {"type": "string", "description": "email body (plain text or HTML)"},
        }, "required": ["to", "subject", "body"]},
    }},
]

TWILIO_TOOLS = [
    {"type": "function", "function": {
        "name": "send_sms",
        "description": "Send an SMS via Twilio on behalf of the user.",
        "parameters": {"type": "object", "properties": {
            "to":      {"type": "string", "description": "recipient phone number with country code, e.g. +2348012345678"},
            "message": {"type": "string"},
        }, "required": ["to", "message"]},
    }},
]

# ── Credential extractor ──────────────────────────────────────────────────────
# Scans user messages for tokens/keys pasted inline — no sidebar, no setup needed.

_CRED_PATTERNS = [
    ("github",  "token",    r"ghp_[A-Za-z0-9]{36,}"),
    ("github",  "token",    r"github_pat_[A-Za-z0-9_]{36,}"),
    ("github",  "token",    r"gho_[A-Za-z0-9]{36,}"),
    ("stripe",  "api_key",  r"sk_(?:live|test)_[A-Za-z0-9]{24,}"),
    ("stripe",  "api_key",  r"rk_(?:live|test)_[A-Za-z0-9]{24,}"),
    ("slack",   "token",    r"xox[bpas]-[A-Za-z0-9\-]{20,}"),
    ("notion",  "token",    r"secret_[A-Za-z0-9]{43,}"),
    ("notion",  "token",    r"ntn_[A-Za-z0-9]{43,}"),
    ("linear",  "api_key",  r"lin_api_[A-Za-z0-9]{40,}"),
    ("resend",  "api_key",  r"re_[A-Za-z0-9]{24,}"),
    # Supabase JWT (anon/service key)
    ("supabase", "anon_key", r"eyJ[A-Za-z0-9_\-]{50,}\.[A-Za-z0-9_\-]{50,}\.[A-Za-z0-9_\-]{20,}"),
]
_SUPABASE_URL_RE  = re.compile(r"https://[a-z0-9]{20,}\.supabase\.co(?:/[^\s\"']*)?")
_TWILIO_SID_RE    = re.compile(r"\bAC[a-f0-9]{32}\b")
_TWILIO_TOKEN_RE  = re.compile(r"\b[a-f0-9]{32}\b")
_TWILIO_PHONE_RE  = re.compile(r"\+\d{7,15}")

def extract_credentials_from_messages(messages: list) -> dict:
    """
    Auto-detects credentials pasted anywhere in user messages.
    Returns a partial integrations dict — same shape as the `integrations` request field.
    Explicit integrations passed in the request always win over extracted ones.
    """
    text = "\n".join(m.get("content", "") for m in messages if m.get("role") == "user")
    creds: dict = {}

    for service, field, pattern in _CRED_PATTERNS:
        m = re.search(pattern, text)
        if m:
            creds.setdefault(service, {})[field] = m.group(0)

    # Supabase URL (separate from JWT key)
    m = _SUPABASE_URL_RE.search(text)
    if m:
        creds.setdefault("supabase", {})["url"] = m.group(0).rstrip("/")

    # Twilio: SID starts with AC + 32-hex auth token + from-number
    sid_m = _TWILIO_SID_RE.search(text)
    if sid_m:
        creds.setdefault("twilio", {})["account_sid"] = sid_m.group(0)
        tok_m = _TWILIO_TOKEN_RE.search(text.replace(sid_m.group(0), ""))
        if tok_m:
            creds["twilio"]["auth_token"] = tok_m.group(0)
        phone_m = _TWILIO_PHONE_RE.search(text)
        if phone_m:
            creds["twilio"]["from_number"] = phone_m.group(0)

    return creds


def build_tools(integrations: dict) -> list:
    tools = list(BASE_TOOLS)
    if "github"   in integrations: tools.extend(GITHUB_TOOLS)
    if "stripe"   in integrations: tools.extend(STRIPE_TOOLS)
    if "supabase" in integrations: tools.extend(SUPABASE_TOOLS)
    if "slack"    in integrations: tools.extend(SLACK_TOOLS)
    if "notion"   in integrations: tools.extend(NOTION_TOOLS)
    if "linear"   in integrations: tools.extend(LINEAR_TOOLS)
    if "resend"   in integrations: tools.extend(RESEND_TOOLS)
    if "twilio"   in integrations: tools.extend(TWILIO_TOOLS)
    if "http_api" in integrations: tools.extend(HTTP_API_TOOLS)
    return tools


# ── Base tool implementations ──────────────────────────────────────────────────

def _search_tavily(query, num_results):
    from tavily import TavilyClient
    r = TavilyClient(api_key=TAVILY_API_KEY).search(query=query, max_results=min(num_results, 10))
    return "\n\n---\n\n".join(
        f"**{x.get('title','')}**\n{x.get('url','')}\n{x.get('content','')}"
        for x in r.get("results", [])
    )

def _search_ddg_instant(query):
    encoded = urllib.parse.quote_plus(query)
    url = f"https://api.duckduckgo.com/?q={encoded}&format=json&no_html=1&skip_disambig=1"
    with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"}), timeout=8) as r:
        data = json.loads(r.read())
    parts = []
    if data.get("Answer"):   parts.append(f"**Direct Answer:** {data['Answer']}")
    if data.get("Abstract"): parts.append(f"**Summary:** {data['Abstract']}\nSource: {data.get('AbstractURL','')}")
    for t in data.get("RelatedTopics", [])[:3]:
        if isinstance(t, dict) and t.get("Text"): parts.append(f"- {t['Text']}")
    return "\n\n".join(parts)

def _search_wikipedia(query, num_results):
    encoded = urllib.parse.quote_plus(query)
    url = f"https://en.wikipedia.org/w/api.php?action=query&list=search&srsearch={encoded}&format=json&srlimit={num_results}&srprop=snippet"
    with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "AnaModel/1.0"}), timeout=8) as r:
        data = json.loads(r.read())
    return "\n\n---\n\n".join(
        f"**{x['title']}** (Wikipedia)\nhttps://en.wikipedia.org/wiki/{urllib.parse.quote(x['title'])}\n{re.sub(r'<[^>]+>','',x.get('snippet',''))}"
        for x in data.get("query", {}).get("search", [])
    )

def _search_google_news(query, num_results=8):
    import html as htmllib
    encoded = urllib.parse.quote_plus(query)
    url = f"https://news.google.com/rss/search?q={encoded}&hl=en&gl=US&ceid=US:en"
    with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "AnaModel/1.0"}), timeout=10) as r:
        content = r.read().decode("utf-8", errors="replace")
    results = []
    for item in re.findall(r"<item>(.*?)</item>", content, re.DOTALL)[:num_results]:
        title_m = re.search(r"<title>(.*?)</title>", item, re.DOTALL)
        desc_m  = re.search(r"<description>(.*?)</description>", item, re.DOTALL)
        pub_m   = re.search(r"<pubDate>(.*?)</pubDate>", item)
        if title_m:
            title = htmllib.unescape(re.sub(r"<[^>]+>", "", title_m.group(1))).strip()
            desc  = htmllib.unescape(re.sub(r"<[^>]+>", "", (desc_m.group(1) if desc_m else ""))).strip()
            pub   = pub_m.group(1).strip()[:16] if pub_m else ""
            line  = f"**{title}**" + (f" ({pub})" if pub else "")
            if desc and desc != title: line += f"\n  {desc[:200]}"
            results.append(line)
    return "\n\n".join(results)

def _get_news(topic=""):
    queries = {"technology":"technology news","business":"business finance news","sports":"sports news",
               "science":"science news","world":"world news","nigeria":"Nigeria news today",
               "uk":"UK news today","us":"United States news today","africa":"Africa news today"}
    q = queries.get(topic.lower().strip(), topic if topic else "top news today")
    r = _search_google_news(q, 10)
    return f"Latest news for '{q}':\n\n{r}" if r else "No news found."

def _web_search(query, num_results=6):
    if TAVILY_API_KEY:
        try:
            r = _search_tavily(query, num_results)
            if r: return r
        except Exception: pass
    try:
        r = _search_google_news(query, num_results)
        if r: return r
    except Exception: pass
    parts = []
    try:
        r = _search_ddg_instant(query)
        if r: parts.append(r)
    except Exception: pass
    try:
        r = _search_wikipedia(query, num_results)
        if r: parts.append(r)
    except Exception: pass
    return "\n\n===\n\n".join(parts) if parts else "No search results found."

def _get_weather(location):
    try:
        geo_url = f"https://geocoding-api.open-meteo.com/v1/search?name={urllib.parse.quote(location)}&count=1&format=json"
        with urllib.request.urlopen(urllib.request.Request(geo_url, headers={"User-Agent":"AnaModel/1.0"}), timeout=8) as r:
            geo = json.loads(r.read())
        if not geo.get("results"): return f"Location not found: {location}"
        p = geo["results"][0]
        lat, lon, name = p["latitude"], p["longitude"], f"{p['name']}, {p.get('country','')}"
        wx_url = (f"https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}"
                  f"&current=temperature_2m,apparent_temperature,weathercode,windspeed_10m,precipitation"
                  f"&hourly=temperature_2m,weathercode,precipitation_probability"
                  f"&daily=temperature_2m_max,temperature_2m_min,sunrise,sunset,precipitation_probability_max"
                  f"&forecast_days=1&timezone=auto")
        with urllib.request.urlopen(urllib.request.Request(wx_url, headers={"User-Agent":"AnaModel/1.0"}), timeout=8) as r:
            wx = json.loads(r.read())
        WMO = {0:"Clear sky",1:"Mainly clear",2:"Partly cloudy",3:"Overcast",45:"Foggy",
               51:"Light drizzle",53:"Drizzle",55:"Heavy drizzle",61:"Light rain",63:"Rain",
               65:"Heavy rain",71:"Light snow",73:"Snow",75:"Heavy snow",80:"Rain showers",
               81:"Moderate showers",82:"Heavy showers",95:"Thunderstorm"}
        cur = wx["current"]; daily = wx["daily"]; hourly = wx["hourly"]
        cond = WMO.get(cur["weathercode"], f"Code {cur['weathercode']}")
        hrs = [f"  {h.split('T')[1]} — {hourly['temperature_2m'][i]}°C, {WMO.get(hourly['weathercode'][i],'')}, {hourly['precipitation_probability'][i]}% rain"
               for i, h in enumerate(hourly["time"])
               if 7 <= int(h.split("T")[1].split(":")[0]) <= 21 and i % 2 == 0]
        return (f"Weather for {name}\nCurrent: {cur['temperature_2m']}°C (feels like {cur['apparent_temperature']}°C), {cond}\n"
                f"Wind: {cur['windspeed_10m']} km/h | Precipitation: {cur['precipitation']}mm\n"
                f"Today: Low {daily['temperature_2m_min'][0]}°C / High {daily['temperature_2m_max'][0]}°C\n"
                f"Sunrise: {daily['sunrise'][0].split('T')[1]} | Sunset: {daily['sunset'][0].split('T')[1]}\n"
                f"Max rain chance: {daily['precipitation_probability_max'][0]}%\n\nHourly:\n{chr(10).join(hrs)}")
    except Exception as e:
        return f"Weather error: {e}"

def _get_crypto_price(coins):
    url = f"https://api.coingecko.com/api/v3/simple/price?ids={urllib.parse.quote(coins.lower().strip())}&vs_currencies=usd&include_24hr_change=true&include_market_cap=true"
    with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent":"AnaModel/1.0"}), timeout=8) as r:
        data = json.loads(r.read())
    if not data: return f"Prices not found for: {coins}"
    return "\n".join(
        f"**{c.upper()}**: ${info.get('usd',0):,.2f} {'↑' if info.get('usd_24h_change',0)>0 else '↓'} {info.get('usd_24h_change',0):.2f}% (24h)"
        for c, info in data.items()
    )


# ── GitHub tool implementations ────────────────────────────────────────────────

def _github_request(method, path, token, body=None):
    url = f"https://api.github.com{path}"
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github+json",
        "User-Agent": "AnaModel/1.0",
        "Content-Type": "application/json",
        "X-GitHub-Api-Version": "2022-11-28",
    })
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        return {"error": e.reason, "status": e.code, "detail": e.read().decode()}

def _github_list_repos(token, per_page=10):
    repos = _github_request("GET", f"/user/repos?per_page={per_page}&sort=updated", token)
    if isinstance(repos, list):
        return "\n".join(f"- **{r['full_name']}** — {r.get('description','') or ''} ({'private' if r['private'] else 'public'})"
                         for r in repos)
    return str(repos)

def _github_list_issues(token, repo, state="open"):
    issues = _github_request("GET", f"/repos/{repo}/issues?state={state}&per_page=20", token)
    if isinstance(issues, list):
        return "\n".join(f"- #{i['number']} **{i['title']}** by @{i['user']['login']} — {i['html_url']}"
                         for i in issues if "pull_request" not in i)
    return str(issues)

def _github_create_issue(token, repo, title, body=""):
    r = _github_request("POST", f"/repos/{repo}/issues", token, {"title": title, "body": body})
    if "html_url" in r:
        return f"Issue created: {r['html_url']}\nNumber: #{r['number']}"
    return str(r)

def _github_read_file(token, repo, path, branch="main"):
    r = _github_request("GET", f"/repos/{repo}/contents/{path}?ref={branch}", token)
    if "content" in r:
        import base64
        content = base64.b64decode(r["content"]).decode("utf-8", errors="replace")
        return f"File: {repo}/{path} (branch: {branch})\n\n{content[:4000]}"
    return str(r)

def _github_search_code(token, query):
    r = _github_request("GET", f"/search/code?q={urllib.parse.quote(query)}&per_page=5", token)
    if "items" in r:
        return "\n".join(f"- **{i['repository']['full_name']}/{i['path']}** — {i['html_url']}"
                         for i in r["items"])
    return str(r)

def _github_create_pr(token, repo, title, head, base="main", body=""):
    r = _github_request("POST", f"/repos/{repo}/pulls", token,
                         {"title": title, "head": head, "base": base, "body": body})
    if "html_url" in r:
        return f"PR created: {r['html_url']}\nNumber: #{r['number']}"
    return str(r)


# ── Stripe tool implementations ────────────────────────────────────────────────

def _stripe_request(method, path, api_key, data=None):
    url = f"https://api.stripe.com{path}"
    body = urllib.parse.urlencode(data).encode() if data else None
    req = urllib.request.Request(url, data=body, method=method, headers={
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/x-www-form-urlencoded",
    })
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        return {"error": json.loads(e.read()).get("error", {})}

def _stripe_get_balance(api_key):
    r = _stripe_request("GET", "/v1/balance", api_key)
    if "available" in r:
        avail = ", ".join(f"{b['amount']/100:.2f} {b['currency'].upper()}" for b in r["available"])
        pending = ", ".join(f"{b['amount']/100:.2f} {b['currency'].upper()}" for b in r["pending"])
        return f"**Available:** {avail}\n**Pending:** {pending}"
    return str(r)

def _stripe_list_payments(api_key, limit=10):
    r = _stripe_request("GET", f"/v1/payment_intents?limit={limit}", api_key)
    if "data" in r:
        return "\n".join(
            f"- {p['id']}: {p['amount']/100:.2f} {p['currency'].upper()} — {p['status']} ({datetime.datetime.fromtimestamp(p['created']).strftime('%Y-%m-%d')})"
            for p in r["data"]
        )
    return str(r)

def _stripe_get_customer(api_key, query):
    if query.startswith("cus_"):
        r = _stripe_request("GET", f"/v1/customers/{query}", api_key)
    else:
        r = _stripe_request("GET", f"/v1/customers/search?query=email:'{urllib.parse.quote(query)}'", api_key)
        if "data" in r and r["data"]:
            r = r["data"][0]
    if "id" in r:
        return f"**{r.get('name','(no name)')}** ({r.get('email','')})\nID: {r['id']}\nCreated: {datetime.datetime.fromtimestamp(r['created']).strftime('%Y-%m-%d')}"
    return str(r)

def _stripe_create_refund(api_key, payment_intent_id, amount=None):
    data = {"payment_intent": payment_intent_id}
    if amount: data["amount"] = str(amount)
    r = _stripe_request("POST", "/v1/refunds", api_key, data)
    if "id" in r:
        return f"Refund created: {r['id']}\nAmount: {r['amount']/100:.2f} {r['currency'].upper()}\nStatus: {r['status']}"
    return str(r)


# ── Supabase tool implementations ──────────────────────────────────────────────

def _supabase_request(method, path, url_base, anon_key, body=None):
    url = f"{url_base.rstrip('/')}{path}"
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "apikey": anon_key,
        "Authorization": f"Bearer {anon_key}",
        "Content-Type": "application/json",
        "Prefer": "return=representation",
    })
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read() or b"[]")
    except urllib.error.HTTPError as e:
        return {"error": e.read().decode()}

def _supabase_list_tables(sb_url, anon_key):
    r = _supabase_request("GET", "/rest/v1/?select=*", sb_url, anon_key)
    if isinstance(r, dict) and "definitions" in r:
        return "Tables: " + ", ".join(r["definitions"].keys())
    # Try information_schema
    r = _supabase_request("GET", "/rest/v1/rpc/get_tables", sb_url, anon_key)
    return str(r)[:2000]

def _supabase_query(sb_url, anon_key, table, select="*", filter_str="", limit=20):
    path = f"/rest/v1/{table}?select={select}&limit={limit}"
    if filter_str and "=" in filter_str:
        k, v = filter_str.split("=", 1)
        path += f"&{k.strip()}=eq.{v.strip()}"
    r = _supabase_request("GET", path, sb_url, anon_key)
    if isinstance(r, list):
        if not r: return "No rows found."
        return f"{len(r)} rows:\n" + json.dumps(r[:10], indent=2)[:3000]
    return str(r)

def _supabase_insert(sb_url, anon_key, table, data):
    r = _supabase_request("POST", f"/rest/v1/{table}", sb_url, anon_key, data)
    if isinstance(r, list) and r:
        return f"Inserted row:\n{json.dumps(r[0], indent=2)}"
    return str(r)

def _supabase_update(sb_url, anon_key, table, data, filter_str):
    path = f"/rest/v1/{table}"
    if filter_str and "=" in filter_str:
        k, v = filter_str.split("=", 1)
        path += f"?{k.strip()}=eq.{v.strip()}"
    r = _supabase_request("PATCH", path, sb_url, anon_key, data)
    return f"Updated: {json.dumps(r, indent=2)[:1000]}"


# ── Slack tool implementations ─────────────────────────────────────────────────

def _slack_request(method, path, token, body=None):
    url = f"https://slack.com/api{path}"
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        return {"ok": False, "error": e.read().decode()}

def _slack_list_channels(token):
    r = _slack_request("GET", "/conversations.list?limit=20&exclude_archived=true", token)
    if r.get("ok") and "channels" in r:
        return "\n".join(f"- #{c['name']} (id: {c['id']}, {c.get('num_members',0)} members)"
                         for c in r["channels"])
    return r.get("error", str(r))

def _slack_send_message(token, channel, text):
    r = _slack_request("POST", "/chat.postMessage", token, {"channel": channel, "text": text})
    if r.get("ok"):
        return f"Message sent to {channel}. Timestamp: {r.get('ts')}"
    return r.get("error", str(r))

def _slack_get_messages(token, channel, limit=10):
    r = _slack_request("GET", f"/conversations.history?channel={channel}&limit={limit}", token)
    if r.get("ok") and "messages" in r:
        return "\n".join(f"[{datetime.datetime.fromtimestamp(float(m['ts'])).strftime('%H:%M')}] {m.get('text','')}"
                         for m in r["messages"])
    return r.get("error", str(r))


# ── Notion tool implementations ────────────────────────────────────────────────

def _notion_request(method, path, token, body=None):
    url = f"https://api.notion.com/v1{path}"
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": f"Bearer {token}",
        "Notion-Version": "2022-06-28",
        "Content-Type": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        return {"error": e.read().decode()}

def _notion_search(token, query):
    r = _notion_request("POST", "/search", token, {"query": query, "page_size": 10})
    if "results" in r:
        items = []
        for obj in r["results"]:
            title = ""
            if obj["object"] == "page":
                props = obj.get("properties", {})
                for k, v in props.items():
                    if v.get("type") == "title" and v.get("title"):
                        title = v["title"][0].get("plain_text", "")
                        break
            items.append(f"- **{title or '(untitled)'}** (id: {obj['id']}, type: {obj['object']})")
        return "\n".join(items) if items else "No results found."
    return str(r)

def _notion_create_page(token, parent_id, title, content=""):
    body = {
        "parent": {"database_id": parent_id} if len(parent_id.replace("-","")) == 32 else {"page_id": parent_id},
        "properties": {"title": {"title": [{"text": {"content": title}}]}},
        "children": [{"object":"block","type":"paragraph","paragraph":{"rich_text":[{"text":{"content":content[:2000]}}]}}] if content else [],
    }
    r = _notion_request("POST", "/pages", token, body)
    if "id" in r:
        return f"Page created: {r['id']}\nURL: {r.get('url','')}"
    return str(r)

def _notion_get_page(token, page_id):
    page = _notion_request("GET", f"/pages/{page_id}", token)
    blocks = _notion_request("GET", f"/blocks/{page_id}/children?page_size=20", token)
    title = ""
    for v in page.get("properties", {}).values():
        if v.get("type") == "title" and v.get("title"):
            title = v["title"][0].get("plain_text", "")
            break
    content_parts = []
    for b in blocks.get("results", []):
        t = b.get("type","")
        rt = b.get(t, {}).get("rich_text", [])
        text = "".join(x.get("plain_text","") for x in rt)
        if text: content_parts.append(text)
    return f"**{title}**\n\n" + "\n".join(content_parts)


# ── Linear tool implementations ────────────────────────────────────────────────

def _linear_graphql(api_key, query, variables=None):
    data = json.dumps({"query": query, "variables": variables or {}}).encode()
    req = urllib.request.Request("https://api.linear.app/graphql", data=data, method="POST", headers={
        "Authorization": api_key,
        "Content-Type": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        return {"errors": [{"message": e.read().decode()}]}

def _linear_list_issues(api_key, state="", limit=10):
    filter_str = f'filter: {{state: {{name: {{eq: "{state}"}}}}}}\n' if state else ""
    q = f"""query {{ issues({filter_str}first: {limit} orderBy: updatedAt) {{
        nodes {{ id title state {{ name }} priority assignee {{ name }} url }} }} }}"""
    r = _linear_graphql(api_key, q)
    issues = r.get("data", {}).get("issues", {}).get("nodes", [])
    return "\n".join(f"- [{i['state']['name']}] **{i['title']}** (p{i['priority']}) — {i['url']}"
                     for i in issues) or "No issues found."

def _linear_create_issue(api_key, title, description="", priority=3):
    # Get first team ID
    teams_r = _linear_graphql(api_key, "query { teams { nodes { id name } } }")
    team_id = teams_r.get("data", {}).get("teams", {}).get("nodes", [{}])[0].get("id", "")
    if not team_id: return "Could not find a team to create the issue in."
    q = """mutation CreateIssue($input: IssueCreateInput!) { issueCreate(input: $input) { issue { id title url } } }"""
    r = _linear_graphql(api_key, q, {"input": {"teamId": team_id, "title": title, "description": description, "priority": priority}})
    issue = r.get("data", {}).get("issueCreate", {}).get("issue", {})
    return f"Issue created: {issue.get('title','')}\nURL: {issue.get('url','')}" if issue else str(r)

def _linear_list_projects(api_key):
    q = "query { projects { nodes { id name state description url } } }"
    r = _linear_graphql(api_key, q)
    projects = r.get("data", {}).get("projects", {}).get("nodes", [])
    return "\n".join(f"- **{p['name']}** [{p['state']}] — {p['url']}"
                     for p in projects) or "No projects found."


# ── Resend (email) implementation ──────────────────────────────────────────────

def _send_email(api_key, from_email, to, subject, body):
    if not from_email:
        from_email = "AnaModel <onboarding@resend.dev>"
    payload = {"from": from_email, "to": [to], "subject": subject,
               "html" if "<" in body else "text": body}
    data = json.dumps(payload).encode()
    req = urllib.request.Request("https://api.resend.com/emails", data=data, method="POST", headers={
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            result = json.loads(r.read())
            return f"Email sent! ID: {result.get('id','')}"
    except urllib.error.HTTPError as e:
        return f"Email error: {e.read().decode()}"


# ── Twilio (SMS) implementation ────────────────────────────────────────────────

def _send_sms(account_sid, auth_token, from_number, to, message):
    url = f"https://api.twilio.com/2010-04-01/Accounts/{account_sid}/Messages.json"
    import base64
    credentials = base64.b64encode(f"{account_sid}:{auth_token}".encode()).decode()
    data = urllib.parse.urlencode({"From": from_number, "To": to, "Body": message}).encode()
    req = urllib.request.Request(url, data=data, method="POST", headers={
        "Authorization": f"Basic {credentials}",
        "Content-Type": "application/x-www-form-urlencoded",
    })
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            result = json.loads(r.read())
            return f"SMS sent! SID: {result.get('sid','')} Status: {result.get('status','')}"
    except urllib.error.HTTPError as e:
        return f"SMS error: {e.read().decode()}"


# ── Tool executor ─────────────────────────────────────────────────────────────

def execute_tool(name: str, args: dict, integrations: dict) -> str:
    # Base tools
    if name == "web_search":
        return _web_search(args["query"], int(args.get("num_results", 6)))
    if name == "get_news":
        return _get_news(args.get("topic", ""))
    if name == "get_weather":
        return _get_weather(args["location"])
    if name == "get_crypto_price":
        return _get_crypto_price(args.get("coins", "bitcoin,ethereum"))
    if name == "http_get":
        try:
            import ipaddress
            parsed = urllib.parse.urlparse(args["url"])
            if parsed.scheme not in ("http", "https"):
                return "Error: only http/https URLs are allowed"
            host = parsed.hostname or ""
            blocked = ["localhost", "127.0.0.1", "0.0.0.0", "::1",
                       "169.254.169.254", "metadata.google.internal"]
            if host in blocked:
                return "Error: access to internal/metadata URLs is not allowed"
            try:
                addr = ipaddress.ip_address(host)
                if addr.is_private or addr.is_loopback or addr.is_link_local:
                    return "Error: access to private IP ranges is not allowed"
            except ValueError:
                pass  # hostname, not raw IP — allow
            req = urllib.request.Request(args["url"], headers={"User-Agent": "Mozilla/5.0 (compatible; AnaModel/1.0)"})
            with urllib.request.urlopen(req, timeout=15) as r:
                raw = r.read().decode("utf-8", errors="replace")
            clean = re.sub(r"\s{2,}", " ", re.sub(r"<[^>]+>", " ", raw)).strip()
            return clean[:4000]
        except Exception as e:
            return f"Fetch error: {e}"
    if name == "calculator":
        try:
            safe = {k: getattr(math, k) for k in dir(math) if not k.startswith("_")}
            safe["__builtins__"] = {}
            return str(eval(args["expression"].replace("^", "**"), safe))
        except Exception as e:
            return f"Calculator error: {e}"
    if name == "run_python":
        buf = io.StringIO()
        try:
            safe_globals = {
                "__builtins__": {
                    "abs": abs, "all": all, "any": any, "bool": bool,
                    "dict": dict, "enumerate": enumerate, "filter": filter,
                    "float": float, "int": int, "len": len, "list": list,
                    "map": map, "max": max, "min": min, "print": print,
                    "range": range, "round": round, "set": set, "sorted": sorted,
                    "str": str, "sum": sum, "tuple": tuple, "type": type, "zip": zip,
                },
                "math": math,
                "json": json,
            }
            with contextlib.redirect_stdout(buf):
                exec(args["code"], safe_globals)
            return buf.getvalue() or "(no output)"
        except Exception as e:
            return f"Python error: {e}"
    if name == "get_time":
        return datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")

    # GitHub
    gh = integrations.get("github", {})
    token = gh.get("token", "")
    if name == "github_list_repos":   return _github_list_repos(token, args.get("per_page", 10))
    if name == "github_list_issues":  return _github_list_issues(token, args["repo"], args.get("state", "open"))
    if name == "github_create_issue": return _github_create_issue(token, args["repo"], args["title"], args.get("body", ""))
    if name == "github_read_file":    return _github_read_file(token, args["repo"], args["path"], args.get("branch", "main"))
    if name == "github_search_code":  return _github_search_code(token, args["query"])
    if name == "github_create_pr":    return _github_create_pr(token, args["repo"], args["title"], args["head"], args.get("base", "main"), args.get("body", ""))

    # Stripe
    sk = integrations.get("stripe", {}).get("api_key", "")
    if name == "stripe_get_balance":    return _stripe_get_balance(sk)
    if name == "stripe_list_payments":  return _stripe_list_payments(sk, args.get("limit", 10))
    if name == "stripe_get_customer":   return _stripe_get_customer(sk, args["query"])
    if name == "stripe_create_refund":  return _stripe_create_refund(sk, args["payment_intent_id"], args.get("amount"))

    # Supabase
    sb = integrations.get("supabase", {})
    sb_url, sb_key = sb.get("url", ""), sb.get("anon_key", "")
    if name == "supabase_list_tables": return _supabase_list_tables(sb_url, sb_key)
    if name == "supabase_query":       return _supabase_query(sb_url, sb_key, args["table"], args.get("select", "*"), args.get("filter", ""), args.get("limit", 20))
    if name == "supabase_insert":      return _supabase_insert(sb_url, sb_key, args["table"], args["data"])
    if name == "supabase_update":      return _supabase_update(sb_url, sb_key, args["table"], args["data"], args["filter"])

    # Slack
    sl_token = integrations.get("slack", {}).get("token", "")
    if name == "slack_list_channels":  return _slack_list_channels(sl_token)
    if name == "slack_send_message":   return _slack_send_message(sl_token, args["channel"], args["text"])
    if name == "slack_get_messages":   return _slack_get_messages(sl_token, args["channel"], args.get("limit", 10))

    # Notion
    notion_token = integrations.get("notion", {}).get("token", "")
    if name == "notion_search":      return _notion_search(notion_token, args["query"])
    if name == "notion_create_page": return _notion_create_page(notion_token, args["parent_id"], args["title"], args.get("content", ""))
    if name == "notion_get_page":    return _notion_get_page(notion_token, args["page_id"])

    # Linear
    linear_key = integrations.get("linear", {}).get("api_key", "")
    if name == "linear_list_issues":   return _linear_list_issues(linear_key, args.get("state",""), args.get("limit",10))
    if name == "linear_create_issue":  return _linear_create_issue(linear_key, args["title"], args.get("description",""), args.get("priority",3))
    if name == "linear_list_projects": return _linear_list_projects(linear_key)

    # Resend (email)
    resend = integrations.get("resend", {})
    if name == "send_email": return _send_email(resend.get("api_key",""), resend.get("from_email",""), args["to"], args["subject"], args["body"])

    # Twilio (SMS)
    twilio = integrations.get("twilio", {})
    if name == "send_sms": return _send_sms(twilio.get("account_sid",""), twilio.get("auth_token",""), twilio.get("from_number",""), args["to"], args["message"])

    # Custom HTTP API
    api = integrations.get("http_api", {})
    if name == "api_call":
        base = api.get("base_url", "").rstrip("/")
        auth = api.get("auth_header", "")
        url = base + args["path"]
        body = json.dumps(args.get("body", {})).encode() if args.get("body") else None
        extra_headers = args.get("headers", {})
        headers = {"Content-Type": "application/json", "User-Agent": "AnaModel/1.0"}
        if auth: headers["Authorization"] = auth
        headers.update(extra_headers)
        try:
            req = urllib.request.Request(url, data=body, method=args["method"].upper(), headers=headers)
            with urllib.request.urlopen(req, timeout=15) as r:
                return r.read().decode("utf-8", errors="replace")[:3000]
        except urllib.error.HTTPError as e:
            return f"HTTP {e.code}: {e.read().decode()[:500]}"
        except Exception as e:
            return f"API error: {e}"

    return f"Unknown tool: {name}"


# ── Agent loop ────────────────────────────────────────────────────────────────

BASE_SYSTEM_PROMPT = """You are AnaModel, a highly capable AI assistant. You combine two modes seamlessly:

**Conversational mode** (like ChatGPT):
- Answer questions clearly and helpfully
- Explain concepts, help with writing, brainstorm ideas
- Be warm, direct, and concise — no unnecessary filler
- For factual or current information, always use your search tools rather than guessing

**Agentic mode** (like Claude Code):
- When given a task or goal, plan it out and execute step by step
- Use tools to take real actions: search the web, query databases, create issues, send messages
- After each action, report what happened and move to the next step
- Complete tasks fully — don't stop halfway or just describe what to do

{integration_context}

**How to decide which mode:**
- Simple question or conversation → answer directly, use search if you need current info
- Task with clear steps (create, update, send, find, build) → plan and execute using tools
- You can switch mid-conversation — start chatting then take action when asked

**Connecting services:**
Users do NOT need a sidebar or settings page. They just paste their token or API key in chat:
  "My GitHub token is ghp_xxx, show me my repos"
  "Stripe key: sk_live_xxx — any failed payments?"
  "Here's my Supabase URL and key: ..."
If a user asks how to connect a service, tell them to paste their key/token directly in the message.
If credentials were already provided earlier in the conversation, use them — do not ask again.

**Quality rules:**
- Never make up facts, prices, news, or data — use tools to verify
- Be honest when you don't know something or when a tool fails
- Format responses clearly: use headers for long answers, bullet points for lists, code blocks for code
- Match the user's tone — casual for chat, precise for technical tasks
- Today's date: {today}
/no_think"""

def build_system_prompt(integrations: dict) -> str:
    today = datetime.datetime.utcnow().strftime("%A, %B %d, %Y")
    desc = {
        "github":   "**GitHub** — list repos, read files, create issues, search code, open PRs",
        "stripe":   "**Stripe** — check balances, list payments, look up customers, issue refunds",
        "supabase": "**Supabase** — query tables, insert and update records",
        "slack":    "**Slack** — list channels, send messages, read recent messages",
        "notion":   "**Notion** — search pages, create pages, read page content",
        "linear":   "**Linear** — list/create issues, list projects",
        "resend":   "**Email (Resend)** — send emails on behalf of the user",
        "twilio":   "**SMS (Twilio)** — send SMS messages",
        "http_api": f"**Custom API** ({integrations.get('http_api',{}).get('base_url','')}) — make authenticated API calls",
    }
    if integrations:
        connected = [desc[k] for k in integrations if k in desc]
        ctx = ("**Connected services** — the user has connected these, use the tools to act on their behalf:\n"
               + "\n".join(f"- {c}" for c in connected))
    else:
        ctx = "No external services connected. Use built-in tools (web search, weather, news, crypto, calculator, Python)."
    return BASE_SYSTEM_PROMPT.replace("{integration_context}", ctx).replace("{today}", today)

def _prepare_messages(messages: list, integrations: dict) -> list:
    system_prompt = build_system_prompt(integrations)
    if not messages or messages[0].get("role") != "system":
        return [{"role": "system", "content": system_prompt}, *messages]
    messages[0]["content"] = system_prompt
    return messages

def _run_tool_loop(messages, model, max_tokens, tools, integrations):
    for _ in range(MAX_STEPS - 1):
        response = client.chat.completions.create(
            model=model, messages=messages, tools=tools,
            tool_choice="auto", max_tokens=max_tokens, temperature=0.1,
        )
        msg = response.choices[0].message
        if response.choices[0].finish_reason == "stop" or not msg.tool_calls:
            return messages
        messages.append(msg)
        for tc in msg.tool_calls:
            args = json.loads(tc.function.arguments)
            result = execute_tool(tc.function.name, args, integrations)
            messages.append({"role": "tool", "tool_call_id": tc.id, "content": result})
    return messages

def run_agent(messages, model=DEFAULT_MODEL, max_tokens=1024, integrations=None):
    integrations = {**extract_credentials_from_messages(messages), **(integrations or {})}
    tools = build_tools(integrations)
    messages = _prepare_messages(messages, integrations)
    messages = _run_tool_loop(messages, model, max_tokens, tools, integrations)
    response = client.chat.completions.create(
        model=model, messages=messages, tools=tools,
        tool_choice="none", max_tokens=max_tokens, temperature=0.1,
    )
    content = response.choices[0].message.content or ""
    return re.sub(r"<think>.*?</think>", "", content, flags=re.DOTALL).strip()

TOOL_LABELS = {
    "web_search": "🔍 Searching the web",
    "get_news": "📰 Getting latest news",
    "get_weather": "🌤️ Checking weather",
    "get_crypto_price": "💰 Fetching crypto prices",
    "http_get": "🌐 Fetching URL",
    "calculator": "🧮 Calculating",
    "run_python": "🐍 Running code",
    "get_time": "🕐 Getting time",
    "github_list_repos": "🐙 Listing GitHub repos",
    "github_list_issues": "🐙 Listing GitHub issues",
    "github_create_issue": "🐙 Creating GitHub issue",
    "github_read_file": "🐙 Reading file from GitHub",
    "github_search_code": "🐙 Searching GitHub code",
    "github_create_pr": "🐙 Creating pull request",
    "stripe_get_balance": "💳 Checking Stripe balance",
    "stripe_list_payments": "💳 Listing Stripe payments",
    "stripe_get_customer": "💳 Looking up Stripe customer",
    "stripe_create_refund": "💳 Issuing Stripe refund",
    "supabase_list_tables": "🟢 Listing Supabase tables",
    "supabase_query": "🟢 Querying Supabase",
    "supabase_insert": "🟢 Inserting into Supabase",
    "supabase_update": "🟢 Updating Supabase record",
    "slack_list_channels": "💬 Listing Slack channels",
    "slack_send_message": "💬 Sending Slack message",
    "slack_get_messages": "💬 Reading Slack messages",
    "notion_search": "📝 Searching Notion",
    "notion_create_page": "📝 Creating Notion page",
    "notion_get_page": "📝 Reading Notion page",
    "linear_list_issues": "📐 Listing Linear issues",
    "linear_create_issue": "📐 Creating Linear issue",
    "linear_list_projects": "📐 Listing Linear projects",
    "send_email": "📧 Sending email",
    "send_sms": "📱 Sending SMS",
    "api_call": "🔌 Calling API",
}

def run_agent_stream(messages, model=DEFAULT_MODEL, max_tokens=1024, integrations=None):
    """
    Unified streaming agent: handles simple chat, web search, and multi-step goal execution
    all in one. Streams tool progress in real-time so the user sees what's happening.
    Credentials pasted inline in messages are auto-detected and merged with any explicit integrations.
    """
    integrations = {**extract_credentials_from_messages(messages), **(integrations or {})}
    tools = build_tools(integrations)
    cid = f"chatcmpl-{uuid.uuid4().hex[:12]}"
    created = int(time.time())

    def _chunk(content, finish=None):
        return f"data: {json.dumps({'id':cid,'object':'chat.completion.chunk','created':created,'model':model,'choices':[{'index':0,'delta':{'content':content} if content else {},'finish_reason':finish}]})}\n\n"

    messages = _prepare_messages(messages, integrations)
    think_buf = ""; in_think = False

    for step in range(MAX_STEPS):
        response = client.chat.completions.create(
            model=model, messages=messages, tools=tools,
            tool_choice="auto", max_tokens=max_tokens, temperature=0.1,
        )
        msg = response.choices[0].message
        finish = response.choices[0].finish_reason

        # No tool calls — stream final answer token by token (always, regardless of step)
        if finish == "stop" or not msg.tool_calls:
            stream = client.chat.completions.create(
                model=model, messages=messages, tools=tools,
                tool_choice="none", max_tokens=max_tokens, temperature=0.1, stream=True,
            )
            for chunk in stream:
                token = chunk.choices[0].delta.content or ""
                if not in_think and "<think>" in token:
                    in_think = True; token = token.split("<think>")[0]
                if in_think:
                    think_buf += token
                    if "</think>" in think_buf:
                        in_think = False; token = think_buf.split("</think>", 1)[1]; think_buf = ""
                    else: continue
                if token:
                    yield _chunk(token)
            break

        # Execute tool calls — show progress to user
        messages.append(msg)
        for tc in msg.tool_calls:
            label = TOOL_LABELS.get(tc.function.name, f"⚙️ Using {tc.function.name}")
            args = json.loads(tc.function.arguments)
            # Show what we're doing
            detail = ""
            if "query" in args:     detail = f": *{args['query'][:60]}*"
            elif "location" in args: detail = f": *{args['location']}*"
            elif "channel" in args:  detail = f": *{args['channel']}*"
            elif "repo" in args:     detail = f": *{args['repo']}*"
            elif "table" in args:    detail = f": *{args['table']}*"
            elif "to" in args:       detail = f" to *{args['to']}*"
            yield _chunk(f"{label}{detail}...\n")
            result = execute_tool(tc.function.name, args, integrations)
            messages.append({"role": "tool", "tool_call_id": tc.id, "content": result})

    yield _chunk("", finish="stop")
    yield "data: [DONE]\n\n"


def run_goal_stream(goal: str, integrations: dict = None, model: str = DEFAULT_MODEL, max_tokens: int = 2048):
    """Goal-oriented agent: plans then executes, streaming progress at each step."""
    integrations = {**extract_credentials_from_messages([{"role": "user", "content": goal}]), **(integrations or {})}
    tools = build_tools(integrations)
    cid = f"chatcmpl-{uuid.uuid4().hex[:12]}"
    created = int(time.time())

    def _event(content):
        return f"data: {json.dumps({'id':cid,'object':'chat.completion.chunk','created':created,'model':model,'choices':[{'index':0,'delta':{'content':content},'finish_reason':None}]})}\n\n"

    system = build_system_prompt(integrations) + """

When given a goal:
1. Start by stating your plan in numbered steps
2. Execute each step using tools, narrating what you're doing
3. Report results after each step
4. Summarize what was accomplished at the end
"""
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": f"Goal: {goal}"},
    ]

    yield _event(f"**Goal:** {goal}\n\n")

    think_buf = ""; in_think = False
    step_count = 0

    for _ in range(MAX_STEPS):
        response = client.chat.completions.create(
            model=model, messages=messages, tools=tools,
            tool_choice="auto", max_tokens=max_tokens, temperature=0.1,
        )
        msg = response.choices[0].message
        finish = response.choices[0].finish_reason

        if msg.content:
            clean = re.sub(r"<think>.*?</think>", "", msg.content, flags=re.DOTALL).strip()
            if clean:
                yield _event(clean + "\n\n")

        if finish == "stop" or not msg.tool_calls:
            break

        messages.append(msg)
        for tc in msg.tool_calls:
            step_count += 1
            args = json.loads(tc.function.arguments)
            tool_name = tc.function.name
            yield _event(f"*🔧 Using {tool_name}...*\n")
            result = execute_tool(tool_name, args, integrations)
            messages.append({"role": "tool", "tool_call_id": tc.id, "content": result})

    yield f"data: {json.dumps({'id':cid,'object':'chat.completion.chunk','created':created,'model':model,'choices':[{'index':0,'delta':{},'finish_reason':'stop'}]})}\n\n"
    yield "data: [DONE]\n\n"


# ── FastAPI app ───────────────────────────────────────────────────────────────

app = FastAPI(title="AnaModel Agent", version="2.0.0")

class Message(BaseModel):
    role: str
    content: str

class ChatRequest(BaseModel):
    model: Optional[str] = DEFAULT_MODEL
    messages: list[Message]
    max_tokens: Optional[int] = 1024
    temperature: Optional[float] = 0.1
    stream: Optional[bool] = False
    integrations: Optional[Dict[str, Any]] = None

class GoalRequest(BaseModel):
    goal: str
    model: Optional[str] = DEFAULT_MODEL
    max_tokens: Optional[int] = 2048
    integrations: Optional[Dict[str, Any]] = None

@app.get("/")
def root():
    return {"status": "ok", "model": DEFAULT_MODEL, "version": "2.0.0",
            "chat_ui": "/chat",
            "endpoints": {
                "chat": "POST /v1/chat/completions",
                "goal": "POST /v1/agent/goal",
                "models": "GET /v1/models",
            },
            "base_tools": [t["function"]["name"] for t in BASE_TOOLS],
            "integrations": ["github", "stripe", "supabase", "slack", "notion", "linear", "resend", "twilio", "http_api"]}

@app.get("/v1/models")
def list_models():
    return {"object": "list", "data": [{"id": DEFAULT_MODEL, "object": "model", "owned_by": "anamodel"}]}

@app.post("/v1/agent/goal")
def agent_goal(request: GoalRequest, authorization: Optional[str] = Header(None)):
    """Goal-oriented endpoint: give it a goal, it plans and executes autonomously."""
    return StreamingResponse(
        run_goal_stream(
            goal=request.goal,
            integrations=request.integrations or {},
            model=request.model or DEFAULT_MODEL,
            max_tokens=request.max_tokens or 2048,
        ),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )

@app.get("/chat", response_class=HTMLResponse)
def chat_ui():
    html_path = Path(__file__).parent / "chat_ui.html"
    if html_path.exists():
        return HTMLResponse(html_path.read_text())
    return HTMLResponse("<h1>Chat UI not found. Deploy chat_ui.html alongside agent_server.py</h1>")

@app.post("/v1/chat/completions")
def chat_completions(request: ChatRequest, authorization: Optional[str] = Header(None)):
    messages = [m.model_dump() for m in request.messages]
    model = request.model or DEFAULT_MODEL
    max_tokens = request.max_tokens or 1024
    integrations = request.integrations or {}

    if request.stream:
        return StreamingResponse(
            run_agent_stream(messages, model=model, max_tokens=max_tokens, integrations=integrations),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )
    try:
        content = run_agent(messages, model=model, max_tokens=max_tokens, integrations=integrations)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    return {
        "id": f"chatcmpl-{uuid.uuid4().hex[:8]}", "object": "chat.completion",
        "created": int(time.time()), "model": model,
        "choices": [{"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }

if __name__ == "__main__":
    print("AnaModel Agent Server v2.0 starting on http://0.0.0.0:8080")
    print(f"Chat UI: http://0.0.0.0:8080/chat")
    print(f"Backend: {LITELLM_BASE_URL}")
    print(f"Base tools: {[t['function']['name'] for t in BASE_TOOLS]}")
    print(f"Integrations: github, stripe, supabase, slack, http_api")
    uvicorn.run(app, host="0.0.0.0", port=8080)
