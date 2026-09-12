#!/usr/bin/env python3
"""
Patch n8n workflow 3J5doqEyxA7lT1OO on https://n.worldinspirelab.com:
1. Fix greedy native name extraction in 'Build People Capture Record' (remove separatedNativeName regex).
2. Set onError: continueRegularOutput and neverError: true on 'Wake Antigravity Contact Review Agent'.
3. Update 'Post Antigravity Review Verdict to Slack' to gracefully handle offline/502 responses.
"""

import json
import os
import pathlib
import sys
import urllib.request
import urllib.error

N8N_URL = os.getenv("N8N_URL", "https://n.worldinspirelab.com")
WORKFLOW_ID = os.getenv("N8N_WORKFLOW_ID", "3J5doqEyxA7lT1OO")
ENV_FILE = pathlib.Path(os.getenv("N8N_ENV_FILE", "")) if os.getenv("N8N_ENV_FILE") else pathlib.Path.home() / ".gemini" / "antigravity" / "skills" / "n8n-automation" / ".env"

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
pre_patch_file = backup_dir / f"workflow_{WORKFLOW_ID}_before_native_name_patch.json"
pre_patch_file.write_text(json.dumps(wf, indent=2), encoding="utf-8")
print(f"Saved pre-patch backup to {pre_patch_file}")

# 3. Patch Build People Capture Record
nodes = wf.get("nodes", [])
for node in nodes:
    if node.get("name") == "Build People Capture Record":
        js = node["parameters"]["jsCode"]
        target = """  const separatedNativeName = /(?:^|[\\s([{（「『【,，:：])([\\p{Script=Han}\\p{Script=Hiragana}\\p{Script=Katakana}\\p{Script=Hangul}][\\p{Script=Han}\\p{Script=Hiragana}\\p{Script=Katakana}\\p{Script=Hangul}・·ー]{1,9})(?=$|[\\s)\\]）」。』】,，:：;；|/])/gmu;
  for (const match of text.matchAll(separatedNativeName)) {
    const candidate = accept(match[1] || '');
    if (candidate && !isPlaceholderName(candidate)) return candidate;
  }
  return '';"""
        replacement = """  // Do NOT greedily scan unlabelled conversational text or note bullets for arbitrary Han substrings
  return '';"""
        if target in js:
            node["parameters"]["jsCode"] = js.replace(target, replacement)
            print("Successfully patched nativeNameCandidate in 'Build People Capture Record'!")
        else:
            print("Warning: separatedNativeName target pattern not found in jsCode; checking manual substring...")
            idx = js.find("const separatedNativeName")
            if idx != -1:
                end_idx = js.find("return '';", idx) + len("return '';")
                node["parameters"]["jsCode"] = js[:idx] + "// Do NOT greedily scan unlabelled conversational text or note bullets for arbitrary Han substrings\n  return '';" + js[end_idx:]
                print("Successfully patched nativeNameCandidate via substring index!")

    elif node.get("name") == "Wake Antigravity Contact Review Agent":
        node["onError"] = "continueRegularOutput"
        opts = node.get("parameters", {}).setdefault("options", {})
        opts["neverError"] = True
        opts["response"] = {"response": {"responseFormat": "json"}}
        print("Successfully patched 'Wake Antigravity Contact Review Agent' with onError: continueRegularOutput and neverError: true!")

    elif node.get("name") == "Post Antigravity Review Verdict to Slack":
        node["parameters"]["text"] = r"""={{ (() => {
  const agentOutput = $('Wake Antigravity Contact Review Agent').first()?.json || {};
  const res = $json.review_result || agentOutput.review_result || {};
  const stdout = $json.stdout || agentOutput.stdout || '';
  let verdict = (res.verdict || '').toUpperCase();
  let explanation = res.explanation || '';
  let pageUrl = res.target_page_url || '';
  let pageName = res.target_name || '';
  let ssot = res.ssot_verified ? 'SSOT write verified' : 'SSOT verification pending';

  // Check if agent failed (e.g. 502 Bad Gateway / laptop asleep)
  if (!verdict && !pageUrl && ($json.error || agentOutput.error || agentOutput.message)) {
    const err = $json.error || agentOutput.error || agentOutput.message || 'Webhook unreachable';
    const errMsg = typeof err === 'object' ? (err.message || JSON.stringify(err)) : String(err);
    return `⚠️ *Antigravity Contact Review Engine (Offline)*\n*Status:* \`LAPTOP_ASLEEP_OR_OFFLINE\`\n> Webhook tunnel returned: ${errMsg.slice(0, 150)}\n_Laptop is asleep; please wake MacBook to process queued contact intake._`;
  }

  if (!verdict && stdout) {
    const lines = stdout.split('\n');
    const vLine = lines.find(l => l.startsWith('Verdict:')) || '';
    const eLine = lines.find(l => l.startsWith('Explanation:')) || '';
    verdict = vLine.replace('Verdict:', '').trim();
    explanation = eLine.replace('Explanation:', '').trim();
  }
  const verdictIcons = { 'NO_CHANGE': '⏸️', 'SUPPLEMENT': '✨', 'CORRECT': '✏️', 'MERGE': '🔀', 'CREATE': '➕' };
  const icon = verdictIcons[verdict] || '🤖';
  const targetLine = pageUrl ? `🔗 <${pageUrl}|${pageName || 'Notion Profile'}>` : (pageName ? `👤 ${pageName}` : '');
  const appendedCount = Number($json.appended_count ?? $('Collapse People Image Append Results').first()?.json?.appended_count ?? res.appended_image_count ?? 0);
  const mediaLine = appendedCount > 0 ? `🖼️ Attached ${appendedCount} image(s) to Notion profile.\n` : '';
  return `${icon} *Antigravity Contact Review Engine*\n*Verdict:* \`${verdict || 'COMPLETED'}\`\n${explanation ? '> ' + explanation + '\n' : ''}${targetLine ? targetLine + '\n' : ''}${mediaLine}_${ssot} & audit trail recorded._`;
})() }}"""
        print("Successfully patched 'Post Antigravity Review Verdict to Slack' with offline error handling!")

# 4. Prepare PUT payload
payload = {
    "name": wf.get("name"),
    "nodes": nodes,
    "connections": wf.get("connections"),
    "settings": wf.get("settings", {"executionOrder": "v1"}),
    "staticData": wf.get("staticData"),
}

# Save updated workflow json locally
updated_file = backup_dir / f"workflow_{WORKFLOW_ID}_native_name_patched.json"
updated_file.write_text(json.dumps(payload, indent=2), encoding="utf-8")
print(f"Saved patched workflow to {updated_file}")

# 5. Push PUT update to n8n API
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
    print(f"Successfully updated workflow {WORKFLOW_ID} on {N8N_URL}!")
except urllib.error.HTTPError as e:
    err_body = e.read().decode("utf-8")
    sys.exit(f"Failed to update workflow: HTTP {e.code}: {err_body}")

# 6. Activate workflow
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

print("\n=== n8n People Workflow Patch Deployed Successfully ===")
