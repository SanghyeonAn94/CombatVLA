"""Diagnostic tool for FAILURE / loop / stuck TCs.

Unlike debug_ask_40b (which assumes PASS verify-true and asks "why didn't you finish?"),
this tool asks the VLM to introspect on a stuck/looping run:
  - Did you recognize you were in a repeating cycle?
  - What is the actual screen state?
  - Is this a recoverable loop or a terminal failure?

Usage:
    python debug_ask_failure.py <log_path> [last_image_path]

Saves JSON artifact to bvt_logs/tc_failure_replays/.
"""
import base64
import json
import os
import re
import sys
from datetime import datetime

sys.path.insert(0, "/mnt/e/CombatVLA")
from nikke_bvt.api_client import APIClient, API_CONFIG

CAPTURES_DIR = "/mnt/e/CombatVLA/captures"
REPLAY_DIR = "/mnt/e/CombatVLA/bvt_logs/tc_failure_replays"
ITER_RE = re.compile(
    r"\[(\d+)\]\s+[\d.]+s\s*\|\s*done=(\w+)\s+action=(\S+)\s+target=(.+?)\s+conf=([\d.]+)\s+\((\d+),(\d+)\)\s+\|\s+(bvt_\d+\.png)"
)
REASON_RE = re.compile(r"reason:\s+(.+)")


def enc(path):
    with open(path, "rb") as f:
        return base64.b64encode(f.read()).decode()


def parse_log_iters(log_path):
    """Extract all iters with action / target / image / reason. Returns list."""
    with open(log_path) as f:
        lines = f.readlines()
    out = []
    cur = None
    for ln in lines:
        m = ITER_RE.search(ln)
        if m:
            if cur:
                out.append(cur)
            cur = {
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
        if cur:
            rm = REASON_RE.search(ln)
            if rm:
                cur["reason"] = rm.group(1).strip()
    if cur:
        out.append(cur)
    return out


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    log_path = sys.argv[1]
    iters = parse_log_iters(log_path)
    if not iters:
        print(f"No iters parsed from {log_path}")
        sys.exit(1)
    print(f"[Load] {log_path}: {len(iters)} iters")

    # Determine target image — last iter's image (the screen at kill/stuck time)
    last_iter = iters[-1]
    target_img = sys.argv[2] if len(sys.argv) > 2 else os.path.join(CAPTURES_DIR, last_iter["img"])
    if not os.path.exists(target_img):
        print(f"Missing capture: {target_img}")
        sys.exit(1)

    # Build conversation summary (compact, no images) for context
    iter_summary = []
    for it in iters:
        iter_summary.append(
            f"iter {it['iter_no']}: action={it['action']} target={it['target']} reason={it['reason'][:80]}"
        )
    history_text = "\n  ".join(iter_summary[-30:])  # last 30 iters

    # Build messages
    system = (
        "You are diagnosing a STUCK / LOOPING NIKKE QA test run. The test was killed because it "
        "was suspected to be in an infinite loop. You will see the screen at the moment of kill, "
        "AND a textual summary of all actions performed before that.\n\n"
        "Answer in PLAIN KOREAN prose (no JSON). Be brutally honest."
    )
    debug_question = (
        f"This run had {len(iters)} iterations. Below is the action history (last 30 iters):\n"
        f"  {history_text}\n\n"
        "The attached image is the LAST capture before the run was killed.\n\n"
        "Questions:\n"
        "1. 화면에 실제로 무엇이 보이나? (3줄 이내)\n"
        "2. 이전 행동 기록을 보고, 같은 패턴이 반복된 cycle이 있었는지 식별하라. "
        "있다면 cycle의 길이(K)와 어떤 행동들이 반복됐는지 명시.\n"
        "3. cycle 안에서 한 번이라도 'now I'm in a loop, this is failing' 이라는 인식이 있었는가? "
        "없었다면 왜? (개별 iter 시점에서는 popup 보이면 닫는 게 합리적이라 인식 어려움?)\n"
        "4. 이 cycle은 게임 자체의 문제인가, 모델의 잘못된 판단인가? "
        "(예: dev server 연결 실패 → 게임이 자동 재시작 popup 띄움 → 모델은 popup 닫기만 반복)\n"
        "5. 이런 종류의 cycle을 미리 감지하려면 어떤 룰이 필요한가?\n"
        "6. 이 TC의 verify_question 관점에서 PASS인가 FAIL인가?"
    )

    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": [
            {"type": "text", "text": debug_question},
            {"type": "image_url", "image_url": {
                "url": f"data:image/png;base64,{enc(target_img)}"
            }},
        ]},
    ]

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
    print("VLM DIAGNOSIS:")
    print("=" * 70)
    print(content)
    print("=" * 70)

    os.makedirs(REPLAY_DIR, exist_ok=True)
    ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    log_basename = os.path.splitext(os.path.basename(log_path))[0]
    out_path = os.path.join(REPLAY_DIR, f"failure_replay_{ts}_{log_basename}.json")

    artifact = {
        "ts": datetime.utcnow().isoformat() + "Z",
        "source_log": os.path.abspath(log_path),
        "captured_image": os.path.basename(target_img),
        "iter_count": len(iters),
        "iter_summary": iter_summary,
        "request": {
            "model": API_CONFIG["strategist"]["model"],
            "system": system,
            "user_text": debug_question,
            "image_path": target_img,
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
