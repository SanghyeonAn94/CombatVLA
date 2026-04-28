"""
Diagnostic tool: replay a failed subtask's iteration sequence up to a target
iteration, then ask the VLM in plain Korean to explain its own decision.

The full diagnostic exchange (request + response) is persisted as a single JSON
artifact under bvt_logs/tc_debug_replays/. This is the canonical record for
TC-step debugging — share / archive the JSON file directly.

Usage:
    python debug_ask_40b.py <log_path> <subtask_num> <explain_iter>

Example:
    python debug_ask_40b.py bvt_logs/tc_resume_*/tc_17.log 6 1

Output JSON schema (saved under bvt_logs/tc_debug_replays/):
{
  "ts":             ISO timestamp of replay,
  "source_log":     path to the TC log,
  "subtask_num":    int,
  "explain_iter":   int,
  "subtask_title":  parsed title,
  "verify_question": parsed question,
  "iteration_summary": [{iter_no, action, target, x, y, image, reason}, ...],
  "request": {
     "model":       model id,
     "messages":    full message list (with images base64-stripped to save space),
     "params":      { temperature, top_p, max_tokens, extra_body },
  },
  "response": {
     "content":         VLM reply text (Korean self-reflection),
     "model":           served model id,
     "finish_reason":   str,
     "usage":           {prompt_tokens, completion_tokens, total_tokens} if available,
  }
}
"""
import base64
import glob
import json
import os
import re
import sys
from datetime import datetime

sys.path.insert(0, "/mnt/e/CombatVLA")
from nikke_bvt.api_client import APIClient, API_CONFIG

CAPTURES_DIR = "/mnt/e/CombatVLA/captures"
REPLAY_DIR = "/mnt/e/CombatVLA/bvt_logs/tc_debug_replays"
ITER_RE = re.compile(
    r"\[(\d+)\]\s+[\d.]+s\s*\|\s*done=(\w+)\s+action=(\S+)\s+target=(.+?)\s+conf=([\d.]+)\s+\((\d+),(\d+)\)\s+\|\s+(bvt_\d+\.png)"
)
REASON_RE = re.compile(r"reason:\s+(.+)")


def enc(path):
    with open(path, "rb") as f:
        return base64.b64encode(f.read()).decode()


def parse_subtask_block(log_path, subtask_num):
    """Return (title, verify_q, iterations[]). Each iteration: dict with
    iter_no, done, action, target, conf, x, y, img, reason."""
    with open(log_path) as f:
        lines = f.readlines()

    start_marker = f"[Subtask {subtask_num}]"
    end_marker_pat = re.compile(r"\[Subtask \d+\]|\[TC #\d+\]\s+(PASS|FAIL)")

    in_block = False
    block_lines = []
    for line in lines:
        if start_marker in line and not in_block:
            in_block = True
        elif in_block and end_marker_pat.search(line) and start_marker not in line:
            break
        if in_block:
            block_lines.append(line)

    if not block_lines:
        raise ValueError(f"Subtask {subtask_num} not found in {log_path}")

    title_line = block_lines[0].strip()
    title = title_line.split("]", 1)[1].strip() if "]" in title_line else title_line
    verify_q = ""
    for ln in block_lines:
        if "Verify:" in ln:
            verify_q = ln.split("Verify:", 1)[1].strip()
            break

    iterations = []
    current_iter = None
    for ln in block_lines:
        m = ITER_RE.search(ln)
        if m:
            if current_iter:
                iterations.append(current_iter)
            current_iter = {
                "iter_no": int(m.group(1)),
                "done": m.group(2).lower() == "true",
                "action": m.group(3),
                "target": m.group(4).strip(),
                "conf": float(m.group(5)),
                "x": int(m.group(6)),
                "y": int(m.group(7)),
                "img": m.group(8),
                "reason": "",
            }
            continue
        if current_iter:
            rm = REASON_RE.search(ln)
            if rm:
                current_iter["reason"] = rm.group(1).strip()
    if current_iter:
        iterations.append(current_iter)

    return title, verify_q, iterations


def rebuild_messages(title, verify_q, iterations, explain_iter):
    """Rebuild the messages 40B would have seen up to explain_iter's user msg."""
    checklist = "(checklist omitted — ask about general reasoning)"
    system_content = (
        f"You are executing a QA subtask for NIKKE.\n"
        f"SUBTASK: {title}\n"
        f"VERIFY QUESTION: {verify_q}\n"
        f"CHECKLIST (steps to complete):\n{checklist}\n\n"
        "IMPORTANT RULES:\n"
        "- ABSOLUTE RULE: If VERIFY QUESTION is satisfied by the current screen, "
        "return done=true, action=verify IMMEDIATELY.\n"
        "- If the current screen is a LATER stage than the subtask goal, return done=true.\n"
        "- Do NOT repeat a step whose condition is already met.\n"
        "- action='verify' means the screen already shows the correct final state.\n\n"
        "Normally you return JSON. For this DIAGNOSTIC turn, answer in PLAIN KOREAN prose."
    )

    messages = [{"role": "system", "content": system_content}]

    for it in iterations:
        if it["iter_no"] > explain_iter:
            break
        img_path = os.path.join(CAPTURES_DIR, it["img"])
        if not os.path.exists(img_path):
            raise FileNotFoundError(f"Missing capture: {img_path}")

        if it["iter_no"] < explain_iter:
            messages.append({"role": "user", "content": [
                {"type": "text", "text": "What should I do now?"},
                {"type": "image_url", "image_url": {
                    "url": f"data:image/png;base64,{enc(img_path)}"
                }},
            ]})
            assistant_json = {
                "done": it["done"],
                "action": it["action"],
                "target": it["target"],
                "x": it["x"],
                "y": it["y"],
                "confidence": it["conf"],
                "reason": it["reason"],
            }
            messages.append({"role": "assistant",
                             "content": json.dumps(assistant_json, ensure_ascii=False)})

    # Replicate image-strip behavior of _call_strategist_multiturn
    for i in range(len(messages) - 1):
        msg = messages[i]
        if msg.get("role") == "user" and isinstance(msg.get("content"), list):
            text_parts = [c.get("text", "") for c in msg["content"] if c.get("type") == "text"]
            msg["content"] = " ".join(text_parts) + " [previous screenshot omitted]"

    # The diagnostic question with explain_iter's image
    target_iter = next(it for it in iterations if it["iter_no"] == explain_iter)
    target_img = os.path.join(CAPTURES_DIR, target_iter["img"])
    prior_actions = [
        f"iter {it['iter_no']}: action={it['action']} target={it['target']}"
        for it in iterations if it["iter_no"] < explain_iter
    ]
    prior_str = "\n  ".join(prior_actions) if prior_actions else "(none)"

    decision_str = (
        f"action={target_iter['action']} target={target_iter['target']} "
        f"done={target_iter['done']} reason={target_iter['reason']!r}"
    )

    debug_question = (
        "This is a DIAGNOSTIC query — answer in PLAIN KOREAN prose, not JSON.\n\n"
        f"SUBTASK: {title}\n"
        f"VERIFY QUESTION: {verify_q}\n\n"
        f"Your previous actions in this subtask:\n  {prior_str}\n\n"
        f"At iteration {explain_iter}, looking at the current screen (attached), "
        f"you decided: {decision_str}\n\n"
        "Questions:\n"
        "1. Was the VERIFY QUESTION already satisfied by the current screen at this moment?\n"
        "2. If yes, why did you NOT return done=true?\n"
        "3. Did the conversation history (your previous actions above) inform your decision, "
        "or did you judge only from the current image?\n"
        "4. What should you have done instead, and why?\n"
        "Be brutally honest. This is for improving the pipeline."
    )
    messages.append({"role": "user", "content": [
        {"type": "text", "text": debug_question},
        {"type": "image_url", "image_url": {
            "url": f"data:image/png;base64,{enc(target_img)}"
        }},
    ]})

    return messages


def main():
    if len(sys.argv) < 4:
        print("Usage: python debug_ask_40b.py <log_path> <subtask_num> <explain_iter>")
        sys.exit(1)

    log_path = sys.argv[1]
    # Expand globs if user passes one
    matches = sorted(glob.glob(log_path))
    if matches:
        log_path = matches[-1]
    subtask_num = int(sys.argv[2])
    explain_iter = int(sys.argv[3])

    print(f"[Load] {log_path}")
    title, verify_q, iterations = parse_subtask_block(log_path, subtask_num)
    print(f"[Subtask {subtask_num}] {title}")
    print(f"[Verify] {verify_q}")
    print(f"[Iters]  {len(iterations)} found, explaining iter {explain_iter}")

    messages = rebuild_messages(title, verify_q, iterations, explain_iter)

    api = APIClient.__new__(APIClient)
    APIClient.__init__(api)

    params = {
        "temperature": 0,
        "top_p": 1.0,
        "max_tokens": 2048,
        "extra_body": {"top_k": -1, "chat_template_kwargs": {"enable_thinking": False}},
    }
    resp = api.strategist.chat.completions.create(
        model=API_CONFIG["strategist"]["model"],
        messages=messages,
        **params,
    )
    content = resp.choices[0].message.content
    print("=" * 70)
    print("VLM response:")
    print("=" * 70)
    print(content)
    print("=" * 70)

    # ── Persist request + response as JSON artifact ──
    os.makedirs(REPLAY_DIR, exist_ok=True)
    ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    log_basename = os.path.splitext(os.path.basename(log_path))[0]
    out_path = os.path.join(
        REPLAY_DIR, f"replay_{ts}_{log_basename}_st{subtask_num}_iter{explain_iter}.json"
    )

    # Strip base64 image data from messages for JSON storage (keep image_path reference)
    messages_for_json = []
    for msg in messages:
        if isinstance(msg.get("content"), list):
            content_lite = []
            for part in msg["content"]:
                if part.get("type") == "image_url":
                    content_lite.append({"type": "image_url", "_image_omitted": True})
                else:
                    content_lite.append(part)
            messages_for_json.append({"role": msg["role"], "content": content_lite})
        else:
            messages_for_json.append(msg)

    iter_summary = [
        {
            "iter_no": it["iter_no"],
            "action": it["action"],
            "target": it["target"],
            "x": it["x"], "y": it["y"],
            "image": it["img"],
            "reason": it["reason"],
            "done": it["done"],
            "conf": it["conf"],
        } for it in iterations
    ]

    artifact = {
        "ts": datetime.utcnow().isoformat() + "Z",
        "source_log": os.path.abspath(log_path),
        "subtask_num": subtask_num,
        "explain_iter": explain_iter,
        "subtask_title": title,
        "verify_question": verify_q,
        "iteration_summary": iter_summary,
        "request": {
            "model": API_CONFIG["strategist"]["model"],
            "messages": messages_for_json,
            "params": params,
        },
        "response": {
            "content": content,
            "model": getattr(resp, "model", None),
            "finish_reason": resp.choices[0].finish_reason if resp.choices else None,
            "usage": (
                {
                    "prompt_tokens": resp.usage.prompt_tokens,
                    "completion_tokens": resp.usage.completion_tokens,
                    "total_tokens": resp.usage.total_tokens,
                } if getattr(resp, "usage", None) else None
            ),
        },
    }
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(artifact, fh, ensure_ascii=False, indent=2)
    print(f"\n[Saved] {out_path}")


if __name__ == "__main__":
    main()
