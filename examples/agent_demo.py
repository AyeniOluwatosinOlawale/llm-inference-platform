"""
Full-featured ReAct agent demo against the live LiteLLM / Qwen3-8B endpoint.

Tools:
  calculator       — math expressions
  get_current_time — UTC date/time
  web_search       — mocked search (swap in SerpAPI / Brave etc.)
  read_file        — read a local file by path
  write_file       — write/append content to a file
  run_python       — execute sandboxed Python code
  http_get         — fetch a URL and return the body
  summarize_text   — compress long text to N sentences
  extract_json     — pull structured data from free text
  remember         — store a key/value in agent memory
  recall           — retrieve a stored value by key
"""

import json
import re
import datetime
import math
import io
import sys
import contextlib
import urllib.request
import urllib.error
from pathlib import Path
from openai import OpenAI

client = OpenAI(
    base_url="http://136.65.123.94/v1",
    api_key="sk-b0454325deddc837764f88240c8f7986",
)

# ── In-memory store for remember/recall ──────────────────────────────────────
_memory: dict[str, str] = {}

# ── Tool definitions ──────────────────────────────────────────────────────────

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "calculator",
            "description": "Evaluate a mathematical expression. Supports +,-,*,/,**,sqrt,log,sin,cos,pi,e.",
            "parameters": {
                "type": "object",
                "properties": {
                    "expression": {"type": "string", "description": "e.g. 'sqrt(144) + log(100)'"}
                },
                "required": ["expression"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_current_time",
            "description": "Returns the current UTC date and time.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": "Search the web for information on a topic. Returns a short snippet.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query string"}
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read the contents of a local file. Returns the text content.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Absolute or relative file path"}
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Write or append text content to a local file.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "File path to write"},
                    "content": {"type": "string", "description": "Text to write"},
                    "mode": {"type": "string", "enum": ["write", "append"], "description": "write overwrites; append adds to end"},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_python",
            "description": "Execute a Python code snippet. Returns stdout output. No network or file access inside.",
            "parameters": {
                "type": "object",
                "properties": {
                    "code": {"type": "string", "description": "Python code to run"}
                },
                "required": ["code"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "http_get",
            "description": "Fetch the content of a URL via HTTP GET. Returns up to 2000 characters.",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "Full URL to fetch"},
                    "headers": {"type": "object", "description": "Optional HTTP headers", "additionalProperties": {"type": "string"}},
                },
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "summarize_text",
            "description": "Compress a long block of text into a short summary of N sentences.",
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "The text to summarize"},
                    "sentences": {"type": "integer", "description": "Target number of sentences (default 3)"},
                },
                "required": ["text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "extract_json",
            "description": "Extract structured fields from free-form text. Returns a JSON object.",
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "Source text"},
                    "fields": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Field names to extract, e.g. ['name','email','phone']",
                    },
                },
                "required": ["text", "fields"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "remember",
            "description": "Store a value in agent memory under a key for later retrieval.",
            "parameters": {
                "type": "object",
                "properties": {
                    "key": {"type": "string"},
                    "value": {"type": "string"},
                },
                "required": ["key", "value"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "recall",
            "description": "Retrieve a previously stored value from agent memory by key.",
            "parameters": {
                "type": "object",
                "properties": {
                    "key": {"type": "string"}
                },
                "required": ["key"],
            },
        },
    },
]

# ── Tool executor ─────────────────────────────────────────────────────────────

def execute_tool(name: str, args: dict) -> str:
    if name == "calculator":
        try:
            expr = args["expression"].replace("^", "**")
            safe_globals = {k: getattr(math, k) for k in dir(math) if not k.startswith("_")}
            safe_globals.update({"__builtins__": {}})
            result = eval(expr, safe_globals)
            return str(result)
        except Exception as e:
            return f"Error: {e}"

    elif name == "get_current_time":
        now = datetime.datetime.utcnow()
        return now.strftime("%Y-%m-%d %H:%M:%S UTC (day %j of %Y, %d days until year end: " +
                            str(365 - now.timetuple().tm_yday) + ")")

    elif name == "web_search":
        # Mock — replace body with SerpAPI / Brave / Tavily call
        mock = {
            "qwen3":   "Qwen3 is Alibaba's 2025 open-source LLM series. The 8B model supports 128K context, 119 languages, built-in tool use, and thinking mode.",
            "vllm":    "vLLM is an open-source LLM inference engine using PagedAttention for high throughput. Latest stable: v0.6.x.",
            "litellm": "LiteLLM is an OpenAI-compatible proxy that routes to 100+ LLM providers. Supports load balancing, caching, and cost tracking.",
            "gke":     "Google Kubernetes Engine (GKE) is Google Cloud's managed Kubernetes service. Supports L4/A100/H100 GPU node pools.",
            "keda":    "KEDA is a Kubernetes event-driven autoscaler. Scales pods based on external metrics like Prometheus queries.",
        }
        q = args["query"].lower()
        for key, val in mock.items():
            if key in q:
                return val
        return f"Search results for '{args['query']}': [mock] No specific entry — integrate a real search API for live results."

    elif name == "read_file":
        try:
            return Path(args["path"]).read_text(encoding="utf-8")[:3000]
        except Exception as e:
            return f"Error reading file: {e}"

    elif name == "write_file":
        try:
            mode = "a" if args.get("mode") == "append" else "w"
            Path(args["path"]).write_text(args["content"], encoding="utf-8") if mode == "w" else \
                open(args["path"], "a").write(args["content"])
            return f"Written to {args['path']}"
        except Exception as e:
            return f"Error writing file: {e}"

    elif name == "run_python":
        buf = io.StringIO()
        try:
            with contextlib.redirect_stdout(buf):
                exec(args["code"], {"__builtins__": __builtins__, "math": math, "json": json})
            return buf.getvalue() or "(no output)"
        except Exception as e:
            return f"Error: {e}"

    elif name == "http_get":
        try:
            req = urllib.request.Request(args["url"], headers=args.get("headers", {}))
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.read().decode("utf-8", errors="replace")[:2000]
        except Exception as e:
            return f"HTTP error: {e}"

    elif name == "summarize_text":
        sentences = args.get("sentences", 3)
        # Simple extractive: return first N sentences
        parts = args["text"].replace("\n", " ").split(". ")
        return ". ".join(parts[:sentences]) + ("." if len(parts) > sentences else "")

    elif name == "extract_json":
        # Ask the model itself to extract (recursive call, no tools)
        fields = ", ".join(args["fields"])
        prompt = f"Extract these fields from the text: {fields}\nText: {args['text']}\nReturn only a JSON object."
        r = client.chat.completions.create(
            model="qwen3-vllm",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=300,
            temperature=0,
        )
        return r.choices[0].message.content

    elif name == "remember":
        _memory[args["key"]] = args["value"]
        return f"Stored: {args['key']} = {args['value']}"

    elif name == "recall":
        return _memory.get(args["key"], f"No value stored for key '{args['key']}'")

    return f"Unknown tool: {name}"

# ── Agent loop ────────────────────────────────────────────────────────────────

def run_agent(user_message: str, max_steps: int = 8):
    print(f"\n{'='*60}")
    print(f"USER: {user_message}")
    print('='*60)

    messages = [
        {
            "role": "system",
            "content": (
                "You are a helpful assistant with access to tools. "
                "Use tools when needed to give accurate answers. /no_think"
            ),
        },
        {"role": "user", "content": user_message},
    ]

    for step in range(max_steps):
        response = client.chat.completions.create(
            model="qwen3-vllm",
            messages=messages,
            tools=TOOLS,
            tool_choice="auto",
            max_tokens=512,
            temperature=0.1,
        )

        msg = response.choices[0].message
        finish = response.choices[0].finish_reason

        if finish == "stop" or not msg.tool_calls:
            clean = re.sub(r"<think>.*?</think>", "", msg.content or "", flags=re.DOTALL).strip()
            print(f"\nASSISTANT: {clean}")
            return clean

        messages.append(msg)
        for tc in msg.tool_calls:
            args = json.loads(tc.function.arguments)
            print(f"\n  [Step {step+1}] TOOL → {tc.function.name}({json.dumps(args)})")
            result = execute_tool(tc.function.name, args)
            print(f"  [Step {step+1}] RESULT → {result[:200]}")
            messages.append({
                "role": "tool",
                "tool_call_id": tc.id,
                "content": result,
            })

    print("\nASSISTANT: (max steps reached)")

# ── Run examples ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    # 1. Calculator
    run_agent(
        "If I invest $5,000 at 8% annual interest compounded monthly for 3 years, "
        "how much will I have? Use the calculator."
    )

    # 2. Time + math (multi-tool)
    run_agent(
        "What is today's date and how many days are left in the year? "
        "Also calculate 365 * 24 * 60 to tell me how many minutes are in a year."
    )

    # 3. Search + remember + recall (multi-hop memory)
    run_agent(
        "Search for what vLLM is, remember the result as 'vllm_info', "
        "then recall it and summarize it in 2 sentences."
    )

    # 4. Code execution
    run_agent(
        "Write and run Python code that generates the first 10 Fibonacci numbers "
        "and returns their sum."
    )

    # 5. Extract structured data
    run_agent(
        "Extract the name, email, and company from this text: "
        "Hi, I am Sarah Johnson from Acme Corp. You can reach me at sarah.j@acme.io for the contract details."
    )

    # 6. Write a file then read it back
    run_agent(
        "Write a file at /tmp/agent_test.txt with the content 'Hello from Qwen3 agent!' "
        "then read it back and confirm what it says."
    )

    # 7. Run Python code
    run_agent(
        "Write and run Python code that generates the first 10 Fibonacci numbers "
        "and returns their sum."
    )

    # 8. HTTP fetch (public API)
    run_agent(
        "Fetch the URL https://httpbin.org/get and tell me what IP address made the request."
    )

    # 9. Multi-tool chain: search + calculator + remember
    run_agent(
        "A GPU server costs $2.48 per hour. Use the calculator to find the monthly cost "
        "if run 18 hours a day for 30 days. Remember the result as 'gpu_monthly_cost', "
        "then tell me what percentage of a $5,000 budget that uses."
    )
