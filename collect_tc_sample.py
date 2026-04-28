r"""Bundle a TC run's artifacts into /mnt/e/CombatVLA/sample/tc_<NN>/.

Per-TC artifact set:
  - tc_<NN>.log              : full subtask trace
  - tc_<NN>.mp4              : recording (merged from chunks if needed)
  - results.json             : results/bvt_*.json (full per-iter data)
  - debug/replay_st<S>_iter<N>.json  : debug_ask_40b output for one chosen iter per subtask
  - debug/captures/<bvt_*.png>       : capture images referenced in debug replays

Selection rule for "chosen iter" per subtask:
  - If subtask FAILed → last iteration (the one that caused the give-up).
  - If subtask PASSed → the verify iteration (done=True), so we can ask "why was this PASS valid?".

Usage:
  python collect_tc_sample.py <tc_no> <log_path> [recording_path] [results_json]

Examples:
  python collect_tc_sample.py 60 bvt_logs/tc_60.log /mnt/e/QA\ Records/tc_60.mp4
  python collect_tc_sample.py 60 bvt_logs/tc60_recorded/tc_60.log
"""
import glob
import json
import os
import shutil
import subprocess
import sys

sys.path.insert(0, "/mnt/e/CombatVLA")
from debug_ask_40b import parse_subtask_block, rebuild_messages, REPLAY_DIR  # noqa: E402
from nikke_bvt.api_client import APIClient, API_CONFIG  # noqa: E402

SAMPLE_ROOT = "/mnt/e/CombatVLA/sample"
QA_RECORDS = "/mnt/e/QA Records"
RESULTS_DIR = "/mnt/e/CombatVLA/results"
CAPTURES_DIR = "/mnt/e/CombatVLA/captures"


def find_recording_chunks(prefix: str) -> list:
    """Find sorted chunk files matching prefix in QA Records."""
    pattern = os.path.join(QA_RECORDS, f"{prefix}_*.mp4")
    files = sorted(glob.glob(pattern))
    return files


def merge_chunks(chunks: list, out_path: str) -> bool:
    """Merge mp4 chunks via ffmpeg concat demuxer. Returns True on success."""
    if not chunks:
        return False
    if len(chunks) == 1:
        shutil.copy2(chunks[0], out_path)
        return True
    list_file = out_path + ".concat.txt"
    with open(list_file, "w") as fh:
        for c in chunks:
            fh.write(f"file '{c}'\n")
    cmd = ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", list_file, "-c", "copy", out_path]
    try:
        subprocess.run(cmd, check=True, capture_output=True)
        os.remove(list_file)
        return True
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        print(f"[Merge] FAILED: {e}")
        return False


def find_results_json(tc_no: int) -> str:
    """Find the most recent results JSON containing this TC."""
    candidates = sorted(glob.glob(os.path.join(RESULTS_DIR, "bvt_2*.json")), reverse=True)
    for c in candidates:
        try:
            with open(c) as fh:
                data = json.load(fh)
            for entry in data:
                if str(entry.get("test_case", {}).get("no")) == str(tc_no):
                    return c
        except Exception:
            continue
    return ""


def parse_subtask_results(log_path: str) -> list:
    """Extract (subtask_num, passed: bool, last_iter_no) from log."""
    with open(log_path) as f:
        lines = f.readlines()
    import re
    results = []
    cur_st = None
    cur_last_iter = None
    for ln in lines:
        m = re.match(r"\s*\[Subtask (\d+)\]", ln)
        if m:
            if cur_st is not None and cur_last_iter is not None:
                pass  # Will be finalized when [Result] appears below
            cur_st = int(m.group(1))
            cur_last_iter = None
            continue
        m2 = re.search(r"\[(\d+)\]\s+[\d.]+s\s*\|\s*done=", ln)
        if m2:
            cur_last_iter = int(m2.group(1))
            continue
        m3 = re.match(r"\s*\[Result\]\s+(PASS|FAIL)", ln)
        if m3 and cur_st is not None:
            passed = m3.group(1) == "PASS"
            if cur_last_iter is not None:
                results.append((cur_st, passed, cur_last_iter))
            cur_st = None
            cur_last_iter = None
    return results


def run_debug_replay(log_path: str, subtask_num: int, explain_iter: int, sample_debug_dir: str):
    """Run a debug_ask_40b-style replay and save artifact INSIDE the sample dir."""
    print(f"\n[Debug] Subtask {subtask_num} iter {explain_iter}")
    try:
        title, verify_q, iterations = parse_subtask_block(log_path, subtask_num)
    except Exception as e:
        print(f"  [skip] parse_subtask_block: {e}")
        return None
    if not any(it["iter_no"] == explain_iter for it in iterations):
        if iterations:
            explain_iter = iterations[-1]["iter_no"]
        else:
            print(f"  [skip] no iterations parsed")
            return None

    try:
        messages = rebuild_messages(title, verify_q, iterations, explain_iter)
    except Exception as e:
        print(f"  [skip] rebuild_messages: {e}")
        return None

    api = APIClient.__new__(APIClient)
    APIClient.__init__(api)
    params = {
        "temperature": 0,
        "top_p": 1.0,
        "max_tokens": 2048,
        "extra_body": {"top_k": -1, "chat_template_kwargs": {"enable_thinking": False}},
    }
    try:
        resp = api.strategist.chat.completions.create(
            model=API_CONFIG["strategist"]["model"],
            messages=messages,
            **params,
        )
    except Exception as e:
        print(f"  [skip] VLM call: {e}")
        return None
    content = resp.choices[0].message.content

    # Strip base64 image data from messages for JSON
    target_iter = next(it for it in iterations if it["iter_no"] == explain_iter)
    captured_image = target_iter["img"]
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
            "iter_no": it["iter_no"], "action": it["action"], "target": it["target"],
            "x": it["x"], "y": it["y"], "image": it["img"],
            "reason": it["reason"], "done": it["done"], "conf": it["conf"],
        } for it in iterations
    ]
    artifact = {
        "source_log": os.path.abspath(log_path),
        "subtask_num": subtask_num,
        "explain_iter": explain_iter,
        "subtask_title": title,
        "verify_question": verify_q,
        "captured_image": captured_image,
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
    out_path = os.path.join(sample_debug_dir, f"replay_st{subtask_num}_iter{explain_iter}.json")
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(artifact, fh, ensure_ascii=False, indent=2)

    # Copy capture image too
    cap_src = os.path.join(CAPTURES_DIR, captured_image)
    cap_dst_dir = os.path.join(sample_debug_dir, "captures")
    os.makedirs(cap_dst_dir, exist_ok=True)
    if os.path.exists(cap_src):
        shutil.copy2(cap_src, os.path.join(cap_dst_dir, captured_image))
    print(f"  [saved] {out_path}")
    return out_path


def main():
    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(1)

    tc_no = int(sys.argv[1])
    log_path = sys.argv[2]
    matches = sorted(glob.glob(log_path))
    if matches:
        log_path = matches[-1]

    recording_path = sys.argv[3] if len(sys.argv) > 3 else ""
    results_json = sys.argv[4] if len(sys.argv) > 4 else ""

    sample_dir = os.path.join(SAMPLE_ROOT, f"tc_{tc_no}")
    debug_dir = os.path.join(sample_dir, "debug")
    os.makedirs(debug_dir, exist_ok=True)

    # 1) Copy log
    log_dst = os.path.join(sample_dir, f"tc_{tc_no}.log")
    shutil.copy2(log_path, log_dst)
    print(f"[Log]  → {log_dst}")

    # 2) Recording (provided OR find chunks)
    rec_dst = os.path.join(sample_dir, f"tc_{tc_no}.mp4")
    if recording_path and os.path.exists(recording_path):
        shutil.copy2(recording_path, rec_dst)
        print(f"[Rec]  → {rec_dst}")
    else:
        chunks = find_recording_chunks(f"tc_{tc_no}")
        if chunks:
            print(f"[Rec]  found {len(chunks)} chunk(s); merging...")
            if merge_chunks(chunks, rec_dst):
                print(f"[Rec]  → {rec_dst}")
            else:
                print(f"[Rec]  merge failed; copying first chunk")
                shutil.copy2(chunks[0], rec_dst)
        else:
            print(f"[Rec]  (no recording found for tc_{tc_no})")

    # 3) Results JSON
    if not results_json:
        results_json = find_results_json(tc_no)
    if results_json and os.path.exists(results_json):
        shutil.copy2(results_json, os.path.join(sample_dir, "results.json"))
        print(f"[Res]  → results.json (from {os.path.basename(results_json)})")

    # 4) Debug replays — one per subtask
    subtask_results = parse_subtask_results(log_path)
    print(f"[Debug] {len(subtask_results)} subtasks parsed")
    for st_num, passed, last_iter in subtask_results:
        run_debug_replay(log_path, st_num, last_iter, debug_dir)

    print(f"\n[DONE] Sample → {sample_dir}")


if __name__ == "__main__":
    main()
