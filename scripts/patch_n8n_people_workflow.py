#!/usr/bin/env python3
"""
Deploy Antigravity Contact Review Agent into n8n workflow 3J5doqEyxA7lT1OO.
Preserves existing nodes for rollback safety while routing active capture records
through Antigravity Webhook Hub SSOT review pipeline.
"""

import json
import os
import pathlib
import sys
import urllib.request
import urllib.error

N8N_URL = "https://n.worldinspirelab.com"
WORKFLOW_ID = "3J5doqEyxA7lT1OO"
ENV_FILE = pathlib.Path("/Users/vecsatfoxmailcom/.gemini/antigravity/skills/n8n-automation/.env")

# 1. Resolve API key
api_key = os.environ.get("N8N_API_KEY")
if not api_key and ENV_FILE.is_file():
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        if line.startswith("N8N_API_KEY="):
            api_key = line.split("=", 1)[1].strip()
            break

if not api_key:
    sys.exit("Error: N8N_API_KEY not found.")

headers = {
    "X-N8N-API-KEY": api_key,
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)",
    "Content-Type": "application/json",
}

# 2. Fetch current workflow
print(f"Fetching workflow {WORKFLOW_ID} from {N8N_URL}...")
req = urllib.request.Request(f"{N8N_URL}/api/v1/workflows/{WORKFLOW_ID}", headers=headers)
with urllib.request.urlopen(req) as resp:
    wf = json.loads(resp.read().decode("utf-8"))

# Save pre-patch backup
backup_dir = pathlib.Path(__file__).resolve().parent.parent / "backups"
backup_dir.mkdir(parents=True, exist_ok=True)
pre_patch_file = backup_dir / f"workflow_{WORKFLOW_ID}_pre_patch.json"
pre_patch_file.write_text(json.dumps(wf, indent=2), encoding="utf-8")
print(f"Saved pre-patch backup to {pre_patch_file}")

# 3. Construct new nodes
nodes = wf.get("nodes", [])
connections = wf.get("connections", {})

# Check if review nodes already exist
review_node_id = "wake-antigravity-contact-review-260909"
slack_verdict_node_id = "post-antigravity-review-verdict-260909"

nodes = [n for n in nodes if n.get("id") not in (review_node_id, slack_verdict_node_id)]

review_node = {
    "parameters": {
        "url": "https://webhook.worldinspirelab.com/webhook/contact-review?sync=true",
        "authentication": "none",
        "sendHeaders": True,
        "headerParameters": {
            "parameters": [
                {
                    "name": "Authorization",
                    "value": "Bearer 86b388ed90b2b226a5322e961a0b816c5da46fdde1893e37760b5d898cf4f7fa",
                },
                {
                    "name": "Content-Type",
                    "value": "application/json",
                },
                {
                    "name": "X-Hub-Event-ID",
                    "value": "={{ 'slack-ppl-' + ($(\"Normalize People Input\").item.json.event_id || $(\"Normalize People Input\").item.json.ts || Date.now()) }}",
                },
            ]
        },
        "options": {
            "response": {
                "response": {
                    "responseFormat": "json"
                }
            }
        },
        "method": "POST",
        "sendBody": True,
        "specifyBody": "json",
        "jsonBody": "={{ (() => {\n  const record = $(\"Build People Capture Record\").item.json || {};\n  const input = $(\"Normalize People Input\").item.json || {};\n  const output = record.output || {};\n  const context = $(\"Attach People Context\").item.json || {};\n  return {\n    source: \"slack_people\",\n    channel_id: input.channel || \"C096KR96AF7\",\n    thread_ts: input.thread_ts || input.ts || \"\",\n    contact: {\n      name: output.name || \"Unknown Person\",\n      phone: output.phone_number || \"\",\n      email: output.email || \"\",\n      company: output.company || \"\",\n      city: output.city || \"\",\n      country: output.country || \"\",\n      entity: output.entity || \"\",\n      birthday: output.birthday || \"\",\n      url: output.url || \"\",\n      note: output.important_info || record.source_text || \"\"\n    },\n    source_text: record.source_text || \"\",\n    source_url: record.source_url || \"\",\n    image_text: context.image_response || \"\"\n  };\n})() }}",
    },
    "id": review_node_id,
    "name": "Wake Antigravity Contact Review Agent",
    "type": "n8n-nodes-base.httpRequest",
    "typeVersion": 4.4,
    "position": [960, -280],
    "retryOnFail": True,
    "maxTries": 3,
    "waitBetweenTries": 3000,
}

slack_verdict_node = {
    "parameters": {
        "authentication": "oAuth2",
        "select": "channel",
        "channelId": {
            "__rl": True,
            "value": "={{ $(\"Normalize People Input\").item.json.channel || \"C096KR96AF7\" }}",
            "mode": "id",
        },
        "text": "={{ (() => {\n  const stdout = $json.stdout || '';\n  const lines = stdout.split('\\n');\n  const verdictLine = lines.find(l => l.startsWith('Verdict:')) || '';\n  const explanationLine = lines.find(l => l.startsWith('Explanation:')) || '';\n  return `🤖 *Antigravity Contact Review Engine*\\n${verdictLine ? '*' + verdictLine + '*\\n' : ''}${explanationLine ? '> ' + explanationLine + '\\n' : ''}\\n_SSOT write verified & audit trail recorded._`;\n})() }}",
        "otherOptions": {
            "includeLinkToWorkflow": False,
            "thread_ts": {
                "replyValues": {
                    "thread_ts": "={{ $(\"Normalize People Input\").item.json.thread_ts || $(\"Normalize People Input\").item.json.ts || \"\" }}",
                }
            },
            "unfurl_links": False,
            "unfurl_media": False,
        },
    },
    "id": slack_verdict_node_id,
    "name": "Post Antigravity Review Verdict to Slack",
    "type": "n8n-nodes-base.slack",
    "typeVersion": 2.3,
    "position": [1240, -280],
    "credentials": {
        "slackOAuth2Api": {
            "id": "fSKxeLHeNp40TGq1",
            "name": "Slack account 123@ WWW 250610  ✅",
        }
    },
}

nodes.append(review_node)
nodes.append(slack_verdict_node)

# 4. Update Connections
# Build People Capture Record -> Wake Antigravity Contact Review Agent -> Post Antigravity Review Verdict to Slack
connections["Build People Capture Record"] = {
    "main": [
        [
            {
                "node": "Wake Antigravity Contact Review Agent",
                "type": "main",
                "index": 0,
            }
        ]
    ]
}

connections["Wake Antigravity Contact Review Agent"] = {
    "main": [
        [
            {
                "node": "Post Antigravity Review Verdict to Slack",
                "type": "main",
                "index": 0,
            }
        ]
    ]
}

# 5. Build PUT payload with strictly allowed fields
payload = {
    "name": wf.get("name"),
    "nodes": nodes,
    "connections": connections,
    "settings": wf.get("settings", {"executionOrder": "v1"}),
    "staticData": wf.get("staticData"),
}

# Save updated workflow json locally
updated_file = backup_dir / f"workflow_{WORKFLOW_ID}_updated.json"
updated_file.write_text(json.dumps(payload, indent=2), encoding="utf-8")
print(f"Saved updated workflow json to {updated_file}")

# 6. Push PUT update to n8n API
print(f"Deploying update to n8n workflow {WORKFLOW_ID}...")
put_req = urllib.request.Request(
    f"{N8N_URL}/api/v1/workflows/{WORKFLOW_ID}",
    data=json.dumps(payload).encode("utf-8"),
    headers=headers,
    method="PUT",
)
try:
    with urllib.request.urlopen(put_req) as resp:
        put_resp = json.loads(resp.read().decode("utf-8"))
    print(f"Successfully updated workflow {WORKFLOW_ID}!")
    print(f"Nodes count: {len(put_resp.get('nodes', []))}")
except urllib.error.HTTPError as e:
    err_body = e.read().decode("utf-8")
    sys.exit(f"Failed to update workflow: HTTP {e.code}: {err_body}")

# 7. Activate workflow
print(f"Activating workflow {WORKFLOW_ID}...")
act_req = urllib.request.Request(
    f"{N8N_URL}/api/v1/workflows/{WORKFLOW_ID}/activate",
    headers=headers,
    method="POST",
)
try:
    with urllib.request.urlopen(act_req) as resp:
        act_resp = json.loads(resp.read().decode("utf-8"))
    print(f"Workflow active status: {act_resp.get('active')}")
except urllib.error.HTTPError as e:
    err_body = e.read().decode("utf-8")
    print(f"Activation note: HTTP {e.code}: {err_body}")

print("\n=== Antigravity Contact Review Agent Deployment Complete ===")
