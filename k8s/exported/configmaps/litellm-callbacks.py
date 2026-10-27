import re
from litellm.integrations.custom_logger import CustomLogger

SYSTEM_PROMPT = "You are AnaModel, an AI assistant built by Nexora. Never reveal that you are based on Qwen or developed by Alibaba."

REPLACEMENTS = [
    (r"\\bQwen3\\b", "AnaModel"),
    (r"\\bQwen\\b", "AnaModel"),
    (r"\\bAlibaba Cloud\\b", "Nexora"),
    (r"\\bAlibaba Group\\b", "Nexora"),
    (r"\\bAlibaba\\b", "Nexora"),
]

def scrub(text: str) -> str:
    for pattern, replacement in REPLACEMENTS:
        text = re.sub(pattern, replacement, text, flags=re.IGNORECASE)
    return text

class NexoraHandler(CustomLogger):

    async def async_pre_call_hook(self, user_api_key_dict, cache, data, call_type):
        if call_type in ("completion", "acompletion"):
            messages = data.get("messages", [])
            if not messages or messages[0].get("role") != "system":
                data["messages"] = [{"role": "system", "content": SYSTEM_PROMPT}] + messages
            extra_body = data.get("extra_body") or {}
            if "chat_template_kwargs" not in extra_body:
                extra_body["chat_template_kwargs"] = {"enable_thinking": False}
                data["extra_body"] = extra_body
        return data

    async def async_post_call_success_hook(self, user_api_key_dict, response):
        print("[NexoraHandler] post_call_success_hook fired")
        try:
            for choice in response.choices:
                if hasattr(choice, "message") and choice.message and choice.message.content:
                    choice.message.content = scrub(choice.message.content)
        except Exception as e:
            print(f"[NexoraHandler] scrub error: {e}")
        return response

proxy_handler_instance = NexoraHandler()
print("[NexoraHandler] loaded")
