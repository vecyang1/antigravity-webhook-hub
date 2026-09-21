import json
import logging
import urllib.request
import urllib.error
import asyncio
from typing import Optional, Dict, Any

from hub.alert_filter import _resolve_typesafe_key

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "jev-latest"
BASE_URL = "https://api.typesafe.ai/v1"

def _evaluate_slack_task_sync(message_text: str, context: Optional[Dict[str, Any]] = None, timeout: float = 3.0) -> dict:
    api_key = _resolve_typesafe_key()
    if not api_key:
        logger.warning("No TypeSafe API key found for task triage. Defaulting to autonomous=False.")
        return {"is_autonomous": False, "noul": 0.0, "reason": "No API key"}

    state_payload = {
        "slack_message": message_text,
        "additional_context": context or {}
    }

    eval_request = {
        "state": state_payload,
        "model": DEFAULT_MODEL,
        "questions": {
            "is_autonomous_task": {
                "type": "noul",
                "instructions": (
                    "Evaluate if the following Slack message is a clear, actionable directive that an AI Agent "
                    "can and should execute autonomously (e.g. data research, API interactions, writing content, "
                    "code edits, web scraping). If it requires physical human actions, is too vague to execute "
                    "without back-and-forth, or is just casual chatter, return false."
                ),
                "criteria": {
                    "true": "Clear, actionable task that a software AI agent can complete autonomously.",
                    "false": "Casual chat, requires physical actions (e.g. shipping items), or hopelessly vague."
                }
            }
        }
    }

    endpoint = f"{BASE_URL}/systemone"
    body_bytes = json.dumps(eval_request, ensure_ascii=False).encode("utf-8")
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Connection": "close"
    }

    req = urllib.request.Request(endpoint, data=body_bytes, headers=headers, method="POST")
    
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            answers = data.get("answers", {})
            noul_val = float(answers.get("is_autonomous_task", {}).get("noul", 0.0))
            is_autonomous = noul_val >= 0.45

            return {
                "is_autonomous": is_autonomous,
                "noul": noul_val,
                "reason": f"Jev evaluated is_autonomous_task with noul={noul_val:0.2f}"
            }
    except Exception as e:
        logger.warning("Jev task triage failed: %s", e)
        return {"is_autonomous": False, "noul": 0.0, "reason": f"API Error: {e}"}

async def evaluate_slack_task_async(message_text: str, context: Optional[Dict[str, Any]] = None, timeout: float = 3.0) -> dict:
    """Asynchronously evaluates a Slack message to determine if it should be autonomously executed by an Agent."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        None,
        lambda: _evaluate_slack_task_sync(message_text, context, timeout)
    )
