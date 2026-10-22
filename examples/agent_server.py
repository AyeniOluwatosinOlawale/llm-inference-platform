"""
AnaModel Agent Server
OpenAI-compatible FastAPI endpoint that runs a full ReAct agent loop.
Exposes: POST /v1/chat/completions

Tools available on every request:
  web_search   — DuckDuckGo live search
  http_get     — Fetch any URL (up to 4000 chars)
  calculator   — Math expressions
  run_python   — Execute sandboxed Python code
  get_time     — Current UTC time

Run:
  pip install fastapi uvicorn duckduckgo-search openai
  python examples/agent_server.py
"""

import json
import re
import math
import io
import datetime
import contextlib
import urllib.request
import urllib.error
from typing import Optional

import os
import time
import urllib.parse
import uvicorn
from fastapi import FastAPI, HTTPException, Header
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from openai import OpenAI

# ── Config ────────────────────────────────────────────────────────────────────

LITELLM_BASE_URL = "http://136.65.123.94/v1"
LITELLM_API_KEY  = "sk-b0454325deddc837764f88240c8f7986"
DEFAULT_MODEL    = "AnaModel"
MAX_STEPS        = 10
TAVILY_API_KEY   = os.environ.get("TAVILY_API_KEY", "")

client = OpenAI(base_url=LITELLM_BASE_URL, api_key=LITELLM_API_KEY)

# ── Tools ─────────────────────────────────────────────────────────────────────

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": "Search the web for any information — news, facts, people, companies, events, technology, science, sports, etc. Use this for any question that needs current or factual information.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query"},
                    "num_results": {"type": "integer", "description": "Number of results (default 5)"},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_news",
            "description": "Get the latest news headlines. Use this when the user asks about news, current events, what's happening today, or recent developments.",
            "parameters": {
                "type": "object",
                "properties": {
                    "topic": {"type": "string", "description": "Optional topic filter, e.g. 'technology', 'business', 'sports'. Leave empty for top headlines."},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_crypto_price",
            "description": "Get live cryptocurrency prices. Use this for any question about Bitcoin, Ethereum, or other crypto prices.",
            "parameters": {
                "type": "object",
                "properties": {
                    "coins": {"type": "string", "description": "Comma-separated coin names, e.g. 'bitcoin,ethereum,solana'"},
                },
                "required": ["coins"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "http_get",
            "description": "Fetch the full content of any URL. Use this to read articles, documentation, or any webpage the user mentions.",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "Full URL to fetch"},
                },
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "calculator",
            "description": "Evaluate a mathematical expression. Supports +,-,*,/,**,sqrt,log,sin,cos,pi,e.",
            "parameters": {
                "type": "object",
                "properties": {
                    "expression": {"type": "string", "description": "e.g. 'sqrt(144) + log(100)'"},
                },
                "required": ["expression"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_python",
            "description": "Execute a Python code snippet. Returns stdout. Use for data analysis, string manipulation, or any computation.",
            "parameters": {
                "type": "object",
                "properties": {
                    "code": {"type": "string", "description": "Python code to run"},
                },
                "required": ["code"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_time",
            "description": "Returns the current UTC date and time.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "Get the current weather and today's forecast for any city or location. Use this for any weather-related questions.",
            "parameters": {
                "type": "object",
                "properties": {
                    "location": {"type": "string", "description": "City name or location, e.g. 'Glasgow', 'Lagos, Nigeria', 'New York'"},
                },
                "required": ["location"],
            },
        },
    },
]

# ── Web search ────────────────────────────────────────────────────────────────

def _search_tavily(query: str, num_results: int) -> str:
    from tavily import TavilyClient
    tavily = TavilyClient(api_key=TAVILY_API_KEY)
    response = tavily.search(query=query, max_results=min(num_results, 10))
    results = []
    for r in response.get("results", []):
        results.append(f"**{r.get('title', '')}**\n{r.get('url', '')}\n{r.get('content', '')}")
    return "\n\n---\n\n".join(results) if results else ""


def _search_ddg_instant(query: str) -> str:
    """DuckDuckGo Instant Answer API — no key, no rate limit."""
    encoded = urllib.parse.quote_plus(query)
    url = f"https://api.duckduckgo.com/?q={encoded}&format=json&no_html=1&skip_disambig=1"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=8) as r:
        data = json.loads(r.read())
    parts = []
    if data.get("Answer"):
        parts.append(f"**Direct Answer:** {data['Answer']}")
    if data.get("Abstract"):
        parts.append(f"**Summary:** {data['Abstract']}\nSource: {data.get('AbstractURL', '')}")
    for topic in data.get("RelatedTopics", [])[:3]:
        if isinstance(topic, dict) and topic.get("Text"):
            parts.append(f"- {topic['Text']}")
    return "\n\n".join(parts)


def _search_wikipedia(query: str, num_results: int) -> str:
    """Wikipedia search — no key, no rate limit."""
    encoded = urllib.parse.quote_plus(query)
    url = f"https://en.wikipedia.org/w/api.php?action=query&list=search&srsearch={encoded}&format=json&srlimit={num_results}&srprop=snippet"
    req = urllib.request.Request(url, headers={"User-Agent": "AnaModel/1.0"})
    with urllib.request.urlopen(req, timeout=8) as r:
        data = json.loads(r.read())
    results = []
    for item in data.get("query", {}).get("search", []):
        title = item["title"]
        snippet = re.sub(r"<[^>]+>", "", item.get("snippet", ""))
        results.append(f"**{title}** (Wikipedia)\nhttps://en.wikipedia.org/wiki/{urllib.parse.quote(title)}\n{snippet}")
    return "\n\n---\n\n".join(results)


def _get_weather(location: str) -> str:
    """Live weather via Open-Meteo + geocoding — no API key needed."""
    try:
        # Step 1: geocode the location
        geo_url = f"https://geocoding-api.open-meteo.com/v1/search?name={urllib.parse.quote(location)}&count=1&format=json"
        req = urllib.request.Request(geo_url, headers={"User-Agent": "AnaModel/1.0"})
        with urllib.request.urlopen(req, timeout=8) as r:
            geo = json.loads(r.read())

        if not geo.get("results"):
            return f"Could not find location: {location}"

        place = geo["results"][0]
        lat, lon = place["latitude"], place["longitude"]
        name = f"{place['name']}, {place.get('country', '')}"

        # Step 2: fetch current weather + hourly forecast
        wx_url = (
            f"https://api.open-meteo.com/v1/forecast"
            f"?latitude={lat}&longitude={lon}"
            f"&current=temperature_2m,apparent_temperature,weathercode,windspeed_10m,precipitation"
            f"&hourly=temperature_2m,weathercode,precipitation_probability"
            f"&daily=temperature_2m_max,temperature_2m_min,sunrise,sunset,precipitation_probability_max"
            f"&forecast_days=1&timezone=auto"
        )
        req = urllib.request.Request(wx_url, headers={"User-Agent": "AnaModel/1.0"})
        with urllib.request.urlopen(req, timeout=8) as r:
            wx = json.loads(r.read())

        WMO = {
            0: "Clear sky", 1: "Mainly clear", 2: "Partly cloudy", 3: "Overcast",
            45: "Foggy", 48: "Icy fog", 51: "Light drizzle", 53: "Drizzle",
            55: "Heavy drizzle", 61: "Light rain", 63: "Rain", 65: "Heavy rain",
            71: "Light snow", 73: "Snow", 75: "Heavy snow", 80: "Rain showers",
            81: "Moderate showers", 82: "Heavy showers", 95: "Thunderstorm",
        }

        cur = wx["current"]
        daily = wx["daily"]
        hourly = wx["hourly"]
        condition = WMO.get(cur["weathercode"], f"Code {cur['weathercode']}")

        # Build hourly summary (every 2 hrs, daytime only)
        hours_summary = []
        for i, hr in enumerate(hourly["time"]):
            hour_num = int(hr.split("T")[1].split(":")[0])
            if 7 <= hour_num <= 21 and i % 2 == 0:
                c = WMO.get(hourly["weathercode"][i], "")
                t = hourly["temperature_2m"][i]
                p = hourly["precipitation_probability"][i]
                hours_summary.append(f"  {hr.split('T')[1]} — {t}°C, {c}, {p}% rain chance")

        result = f"""Weather for {name}
Current: {cur['temperature_2m']}°C (feels like {cur['apparent_temperature']}°C), {condition}
Wind: {cur['windspeed_10m']} km/h | Precipitation: {cur['precipitation']}mm

Today's range: Low {daily['temperature_2m_min'][0]}°C / High {daily['temperature_2m_max'][0]}°C
Sunrise: {daily['sunrise'][0].split('T')[1]} | Sunset: {daily['sunset'][0].split('T')[1]}
Max rain chance: {daily['precipitation_probability_max'][0]}%

Hourly forecast:
{chr(10).join(hours_summary)}"""
        return result

    except Exception as e:
        return f"Weather error: {e}"


def _search_bing(query: str, num_results: int) -> str:
    """Bing web search — no API key needed."""
    encoded = urllib.parse.quote_plus(query)
    url = f"https://www.bing.com/search?q={encoded}&count={num_results}"
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept": "text/html",
        "Accept-Language": "en-US,en;q=0.9",
    })
    with urllib.request.urlopen(req, timeout=10) as r:
        html = r.read().decode("utf-8", errors="replace")

    results = []
    # Each Bing result is in <li class="b_algo">
    blocks = re.findall(r'<li class="b_algo"(.*?)</li>', html, re.DOTALL)
    for block in blocks[:num_results]:
        h2 = re.search(r'<h2[^>]*>.*?<a[^>]+href="(https?://[^"]+)"[^>]*>(.*?)</a>', block, re.DOTALL)
        snip = re.search(r'<p[^>]*class="[^"]*b_[^"]*"[^>]*>(.*?)</p>|<p>(.*?)</p>', block, re.DOTALL)
        if h2:
            link = h2.group(1)
            title = re.sub(r"<[^>]+>", "", h2.group(2)).strip()
            snippet = ""
            if snip:
                snippet = re.sub(r"<[^>]+>", "", (snip.group(1) or snip.group(2) or "")).strip()
            import html as htmllib
            title = htmllib.unescape(title)
            snippet = htmllib.unescape(snippet)
            if title:
                results.append(f"**{title}**\n{link}\n{snippet}")
    return "\n\n---\n\n".join(results)


def _get_news(topic: str = "") -> str:
    """Latest news from BBC RSS — free, no key."""
    feeds = {
        "technology": "https://feeds.bbci.co.uk/news/technology/rss.xml",
        "business": "https://feeds.bbci.co.uk/news/business/rss.xml",
        "sports": "https://feeds.bbci.co.uk/sport/rss.xml",
        "science": "https://feeds.bbci.co.uk/news/science_and_environment/rss.xml",
        "world": "https://feeds.bbci.co.uk/news/world/rss.xml",
    }
    feed_url = feeds.get(topic.lower().strip(), "https://feeds.bbci.co.uk/news/rss.xml")
    req = urllib.request.Request(feed_url, headers={"User-Agent": "AnaModel/1.0"})
    with urllib.request.urlopen(req, timeout=8) as r:
        content = r.read().decode("utf-8", errors="replace")
    items = re.findall(r"<item>(.*?)</item>", content, re.DOTALL)
    results = []
    for item in items[:8]:
        title_m = re.search(r"<title><!\[CDATA\[(.*?)\]\]>|<title>(.*?)</title>", item, re.DOTALL)
        desc_m = re.search(r"<description><!\[CDATA\[(.*?)\]\]>|<description>(.*?)</description>", item, re.DOTALL)
        link_m = re.search(r"<link>(.*?)</link>", item)
        if title_m:
            title = (title_m.group(1) or title_m.group(2) or "").strip()
            desc = (desc_m.group(1) or desc_m.group(2) or "").strip() if desc_m else ""
            link = link_m.group(1).strip() if link_m else ""
            results.append(f"• **{title}**\n  {desc[:150]}")
    return f"Latest news (BBC):\n\n" + "\n\n".join(results) if results else "No news found."


def _get_crypto_price(coins: str) -> str:
    """Live crypto prices from CoinGecko — free, no key."""
    coin_ids = urllib.parse.quote(coins.lower().strip())
    url = f"https://api.coingecko.com/api/v3/simple/price?ids={coin_ids}&vs_currencies=usd&include_24hr_change=true&include_market_cap=true"
    req = urllib.request.Request(url, headers={"User-Agent": "AnaModel/1.0"})
    with urllib.request.urlopen(req, timeout=8) as r:
        data = json.loads(r.read())
    if not data:
        return f"Could not find prices for: {coins}"
    lines = []
    for coin, info in data.items():
        price = info.get("usd", "N/A")
        change = info.get("usd_24h_change", 0)
        mcap = info.get("usd_market_cap", 0)
        arrow = "↑" if change > 0 else "↓"
        lines.append(f"**{coin.upper()}**: ${price:,.2f} {arrow} {change:.2f}% (24h)  |  Market cap: ${mcap/1e9:.1f}B")
    return "\n".join(lines)


def _web_search(query: str, num_results: int = 5) -> str:
    """Multi-source search: Tavily → Bing → Wikipedia + DDG Instant Answers."""
    # Primary: Tavily (best quality, needs API key)
    if TAVILY_API_KEY:
        try:
            result = _search_tavily(query, num_results)
            if result:
                return result
        except Exception:
            pass

    # Secondary: Bing web search
    try:
        bing = _search_bing(query, num_results)
        if bing:
            return bing
    except Exception:
        pass

    # Fallback: DDG Instant Answers + Wikipedia
    parts = []
    try:
        ddg = _search_ddg_instant(query)
        if ddg:
            parts.append(ddg)
    except Exception:
        pass
    try:
        wiki = _search_wikipedia(query, num_results)
        if wiki:
            parts.append(wiki)
    except Exception:
        pass

    if parts:
        return "\n\n===\n\n".join(parts)
    return "No search results found. Try rephrasing the query."


# ── Tool executor ─────────────────────────────────────────────────────────────

def execute_tool(name: str, args: dict) -> str:
    if name == "web_search":
        return _web_search(args["query"], int(args.get("num_results", 5)))

    elif name == "http_get":
        try:
            req = urllib.request.Request(
                args["url"],
                headers={"User-Agent": "Mozilla/5.0 (compatible; AnaModel/1.0)"},
            )
            with urllib.request.urlopen(req, timeout=15) as r:
                raw = r.read().decode("utf-8", errors="replace")
            # Strip HTML tags for readability
            clean = re.sub(r"<[^>]+>", " ", raw)
            clean = re.sub(r"\s{2,}", " ", clean).strip()
            return clean[:4000]
        except Exception as e:
            return f"Fetch error: {e}"

    elif name == "calculator":
        try:
            expr = args["expression"].replace("^", "**")
            safe = {k: getattr(math, k) for k in dir(math) if not k.startswith("_")}
            safe["__builtins__"] = {}
            return str(eval(expr, safe))
        except Exception as e:
            return f"Calculator error: {e}"

    elif name == "run_python":
        buf = io.StringIO()
        try:
            with contextlib.redirect_stdout(buf):
                exec(args["code"], {"__builtins__": __builtins__, "math": math, "json": json})
            return buf.getvalue() or "(no output)"
        except Exception as e:
            return f"Python error: {e}"

    elif name == "get_time":
        return datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")

    elif name == "get_weather":
        return _get_weather(args["location"])

    elif name == "get_news":
        return _get_news(args.get("topic", ""))

    elif name == "get_crypto_price":
        return _get_crypto_price(args.get("coins", "bitcoin,ethereum"))

    return f"Unknown tool: {name}"

# ── Agent loop ────────────────────────────────────────────────────────────────

SYSTEM_PROMPT = (
    "You are AnaModel, a helpful AI assistant with access to web search, "
    "news, weather, crypto prices, URL fetching, math, and code execution tools. "
    "Always use tools to get current information rather than relying on training data. /no_think"
)

def _prepare_messages(messages: list) -> list:
    if not messages or messages[0].get("role") != "system":
        messages = [{"role": "system", "content": SYSTEM_PROMPT}, *messages]
    elif "/no_think" not in messages[0].get("content", ""):
        messages[0]["content"] += " /no_think"
    return messages

def _run_tool_loop(messages: list, model: str, max_tokens: int) -> list:
    """Run tool calls until the model is ready to give a final answer. Returns updated messages."""
    for _ in range(MAX_STEPS - 1):
        response = client.chat.completions.create(
            model=model, messages=messages, tools=TOOLS,
            tool_choice="auto", max_tokens=max_tokens, temperature=0.1,
        )
        msg = response.choices[0].message
        finish = response.choices[0].finish_reason

        if finish == "stop" or not msg.tool_calls:
            return messages  # ready for final answer

        messages.append(msg)
        for tc in msg.tool_calls:
            args = json.loads(tc.function.arguments)
            result = execute_tool(tc.function.name, args)
            messages.append({"role": "tool", "tool_call_id": tc.id, "content": result})

    return messages

def run_agent(messages: list, model: str = DEFAULT_MODEL, max_tokens: int = 1024) -> str:
    messages = _prepare_messages(messages)
    messages = _run_tool_loop(messages, model, max_tokens)

    response = client.chat.completions.create(
        model=model, messages=messages, tools=TOOLS,
        tool_choice="none", max_tokens=max_tokens, temperature=0.1,
    )
    content = response.choices[0].message.content or ""
    return re.sub(r"<think>.*?</think>", "", content, flags=re.DOTALL).strip()


def run_agent_stream(messages: list, model: str = DEFAULT_MODEL, max_tokens: int = 1024):
    """Stream the final response after running tool loop. Yields SSE chunks."""
    import uuid, time as _time
    cid = f"chatcmpl-{uuid.uuid4().hex[:12]}"
    created = int(_time.time())

    messages = _prepare_messages(messages)
    messages = _run_tool_loop(messages, model, max_tokens)

    # Stream final answer
    stream = client.chat.completions.create(
        model=model, messages=messages, tools=TOOLS,
        tool_choice="none", max_tokens=max_tokens, temperature=0.1, stream=True,
    )

    think_buf = ""
    in_think = False

    for chunk in stream:
        delta = chunk.choices[0].delta
        token = delta.content or ""

        # Strip <think> blocks on the fly
        if not in_think and "<think>" in token:
            in_think = True
            token = token.split("<think>")[0]
        if in_think:
            think_buf += token
            if "</think>" in think_buf:
                in_think = False
                token = think_buf.split("</think>", 1)[1]
                think_buf = ""
            else:
                continue

        if token:
            payload = {
                "id": cid, "object": "chat.completion.chunk", "created": created,
                "model": model,
                "choices": [{"index": 0, "delta": {"content": token}, "finish_reason": None}],
            }
            yield f"data: {json.dumps(payload)}\n\n"

    done_payload = {
        "id": cid, "object": "chat.completion.chunk", "created": created,
        "model": model,
        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
    }
    yield f"data: {json.dumps(done_payload)}\n\n"
    yield "data: [DONE]\n\n"

# ── FastAPI app ───────────────────────────────────────────────────────────────

app = FastAPI(title="AnaModel Agent Server", version="1.0.0")


class Message(BaseModel):
    role: str
    content: str


class ChatRequest(BaseModel):
    model: Optional[str] = DEFAULT_MODEL
    messages: list[Message]
    max_tokens: Optional[int] = 1024
    temperature: Optional[float] = 0.1
    stream: Optional[bool] = False


@app.get("/")
def root():
    return {"status": "ok", "model": DEFAULT_MODEL, "tools": [t["function"]["name"] for t in TOOLS]}


@app.get("/v1/models")
def list_models():
    return {
        "object": "list",
        "data": [{"id": DEFAULT_MODEL, "object": "model", "owned_by": "anamodel"}],
    }


@app.post("/v1/chat/completions")
def chat_completions(request: ChatRequest, authorization: Optional[str] = Header(None)):
    import time as _time
    messages = [m.model_dump() for m in request.messages]
    model = request.model or DEFAULT_MODEL
    max_tokens = request.max_tokens or 1024

    if request.stream:
        return StreamingResponse(
            run_agent_stream(messages, model=model, max_tokens=max_tokens),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    try:
        content = run_agent(messages, model=model, max_tokens=max_tokens)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    return {
        "id": f"chatcmpl-agent",
        "object": "chat.completion",
        "created": int(_time.time()),
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }


if __name__ == "__main__":
    print("AnaModel Agent Server starting on http://0.0.0.0:8080")
    print(f"Backend: {LITELLM_BASE_URL}")
    print(f"Tools: {[t['function']['name'] for t in TOOLS]}")
    print(f"Web search: {'✅ Tavily enabled' if TAVILY_API_KEY else '❌ No TAVILY_API_KEY — set it to enable live search'}")
    uvicorn.run(app, host="0.0.0.0", port=8080)
