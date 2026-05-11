import asyncio
import os
import time
import httpx
from fastapi import FastAPI, Query
from fastapi.responses import JSONResponse, PlainTextResponse

ACTORVLM_URL = os.environ.get("ACTORVLM_URL", "http://actorvlm:8000")
ACTORVLM_KEY = os.environ.get("ACTORVLM_KEY", "combatvla")
HOST_IP = os.environ.get("HOST_IP", "10.213.70.101")
PROBE_TIMEOUT = float(os.environ.get("PROBE_TIMEOUT", "3.0"))

app = FastAPI(title="VLM Stack Proxy + Specs")

# ── Backend service registry ──
# id: (base_url, api_key_or_None, role description)
BACKENDS: dict[str, tuple[str, str | None, str]] = {
    "actor":    (ACTORVLM_URL, ACTORVLM_KEY, "ActorVLM (Qwen3.5-27B) — perception · planning · classify"),
    "actorvlm": (ACTORVLM_URL, ACTORVLM_KEY, "ActorVLM (Qwen3.5-27B) — alias of actor"),
}

# Long-lived HTTP client for proxy forwarding (no timeout — VLM calls can be slow)
_proxy_client: httpx.AsyncClient | None = None


@app.on_event("startup")
async def _startup():
    global _proxy_client
    _proxy_client = httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=5.0))


@app.on_event("shutdown")
async def _shutdown():
    global _proxy_client
    if _proxy_client:
        await _proxy_client.aclose()

SPECS = {
    "model": {
        "served_name": "ActorVLM",
        "root": "Qwen/Qwen3.5-27B",
        "type": "Vision-Language Model (multimodal, image+video+text)",
        "architecture": "Qwen3_5ForConditionalGeneration",
        "params_estimate": "~27.8B",
        "weights_size_gb": 55.5,
        "dtype": "bfloat16",
        "snapshot": "fc05daec18b0a78c049392ed2e771dde82bdf654",
        "text": {
            "hidden_size": 5120,
            "num_hidden_layers": 64,
            "num_attention_heads": 24,
            "num_key_value_heads": 4,
            "head_dim": 256,
            "intermediate_size": 17408,
            "vocab_size": 248320,
            "max_position_embeddings": 262144,
        },
        "vision": {
            "depth": 27,
            "hidden_size": 1152,
            "num_heads": 16,
            "patch_size": 16,
            "spatial_merge_size": 2,
            "temporal_patch_size": 2,
            "out_hidden_size": 5120,
            "in_channels": 3,
        },
    },
    "serving": {
        "engine": "vLLM",
        "engine_version": "0.19.0",
        "image": "docker.io/vllm/vllm-openai:latest",
        "tensor_parallel_size": 1,
        "max_model_len": 131072,
        "gpu_memory_utilization": 0.90,
        "kv_cache_dtype": "auto (bf16)",
        "kv_cache_tokens": 431200,
        "kv_cache_gib": 105.29,
        "attention_backend_runtime": "FLASHINFER",
        "attention_backend_env": "FLASH_ATTN (overridden by vLLM auto-select)",
        "chunked_prefill": True,
        "max_num_batched_tokens": 8192,
        "prefix_caching": False,
        "reasoning_parser": "qwen3",
    },
    "determinism": {
        "server": {
            "seed": 42,
            "enforce_eager": True,
            "CUBLAS_WORKSPACE_CONFIG": ":4096:8",
        },
        "client_required": {
            "temperature": 0,
            "top_p": 1.0,
            "top_k": -1,
            "extra_body": {
                "chat_template_kwargs": {"enable_thinking": False}
            },
        },
        "notes": [
            "FLASH_ATTN env override is ineffective; FLASHINFER backend is used instead.",
            "enforce_eager=True disables CUDA graphs (~30-40% slower; argmax stable).",
            "Reproducibility (bit-exact) not yet measured.",
        ],
    },
    "endpoint": {
        "openai_compat_base_url": f"http://{HOST_IP}:30010/v1",
        "api_key": "combatvla",
        "model": "ActorVLM",
        "deployed_gpu_index": 5,
    },
    "hardware": {
        "gpu": "NVIDIA B200",
        "vram_total_gib": 183,
        "driver": "590.48.01",
        "cuda_in_image": "12.9.1",
        "host_cpu": "Intel Xeon 6960P x2 (288 CPUs / 144 cores HT)",
        "host_ram_tib": 2.2,
    },
    "weights_cache": {
        "host_path": "/data/combatvla_models/hub/models--Qwen--Qwen3.5-27B/",
        "symlink_in_serving": "serving/models/hub/models--Qwen--Qwen3.5-27B → host_path",
    },
    "client_side": {
        "config_module": "nikke_bvt.config",
        "key": "actorvlm",
        "fallback": {
            "key": "actorvlm_fallback",
            "provider": "OpenRouter (qwen/qwen3-vl-32b-instruct)",
            "trigger": "local actorvlm down",
        },
    },
}


async def actorvlm_live() -> dict:
    try:
        async with httpx.AsyncClient(timeout=2.0) as c:
            r = await c.get(
                f"{ACTORVLM_URL}/v1/models",
                headers={"Authorization": f"Bearer {ACTORVLM_KEY}"},
            )
            return {"status": "up", "models": r.json()}
    except Exception as e:
        return {"status": "down", "error": str(e)}


def to_text(d: dict, indent: int = 0) -> str:
    pad = "  " * indent
    out = []
    for k, v in d.items():
        if isinstance(v, dict):
            out.append(f"{pad}{k}:")
            out.append(to_text(v, indent + 1))
        elif isinstance(v, list):
            out.append(f"{pad}{k}:")
            for item in v:
                if isinstance(item, dict):
                    out.append(to_text(item, indent + 1))
                else:
                    out.append(f"{pad}  - {item}")
        else:
            out.append(f"{pad}{k}: {v}")
    return "\n".join(out)


@app.get("/")
async def root():
    return {
        "service": "actorvlm-proxy",
        "endpoints": {
            "introspection": ["/health", "/specs", "/specs?format=text", "/services"],
            "proxy": ["/actor/v1/...  (alias: /actorvlm/v1/...)"],
        },
        "backends": {k: v[0] for k, v in BACKENDS.items()},
    }


@app.get("/health")
async def health():
    return {"ok": True}


@app.get("/specs")
async def specs(format: str = Query(default="json")):
    body = dict(SPECS)
    body["live"] = {"actorvlm": await actorvlm_live()}
    if format == "text":
        return PlainTextResponse(to_text(body))
    return JSONResponse(body)


# ─────────────────────────────────────────────────────────────────────────────
# Service aggregator — gateway / dashboard calls this for VLM stack health
# ─────────────────────────────────────────────────────────────────────────────

async def _probe_backend(client: httpx.AsyncClient, sid: str) -> dict:
    base_url, key, role = BACKENDS[sid]
    # vLLM exposes /v1/models
    path = "/v1/models"
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    t0 = time.monotonic()
    try:
        r = await client.get(f"{base_url}{path}", headers=headers, timeout=PROBE_TIMEOUT)
        latency_ms = int((time.monotonic() - t0) * 1000)
        ok = 200 <= r.status_code < 300
        model = None
        try:
            payload = r.json() if "json" in r.headers.get("content-type", "") else {}
            if isinstance(payload, dict) and payload.get("data"):
                model = payload["data"][0].get("id")
        except Exception:
            pass
        return {
            "id": sid,
            "role": role,
            "endpoint": base_url,
            "status": "healthy" if ok else "degraded",
            "http_status": r.status_code,
            "latency_ms": latency_ms,
            "model": model,
            "error": None if ok else f"HTTP {r.status_code}",
        }
    except httpx.TimeoutException:
        return {
            "id": sid, "role": role, "endpoint": base_url,
            "status": "down", "http_status": None,
            "latency_ms": int(PROBE_TIMEOUT * 1000),
            "model": None, "error": "timeout",
        }
    except Exception as e:
        return {
            "id": sid, "role": role, "endpoint": base_url,
            "status": "down", "http_status": None,
            "latency_ms": int((time.monotonic() - t0) * 1000),
            "model": None, "error": str(e)[:120],
        }


@app.get("/services")
async def services():
    """Aggregate live health of all VLM stack backends. Dedup aliases."""
    seen_urls: set[str] = set()
    unique_ids: list[str] = []
    for sid, (url, _, _) in BACKENDS.items():
        if url in seen_urls:
            continue
        seen_urls.add(url)
        unique_ids.append(sid)

    async with httpx.AsyncClient() as client:
        results = await asyncio.gather(*[_probe_backend(client, sid) for sid in unique_ids])

    summary = {
        "healthy": sum(1 for r in results if r["status"] == "healthy"),
        "degraded": sum(1 for r in results if r["status"] == "degraded"),
        "down": sum(1 for r in results if r["status"] == "down"),
        "total": len(results),
    }
    return {"summary": summary, "services": results}


# ─────────────────────────────────────────────────────────────────────────────
# Proxy — forward client requests to the right backend
# ─────────────────────────────────────────────────────────────────────────────

_HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade", "host", "content-length",
}


async def _forward(backend_id: str, sub_path: str, request) -> JSONResponse:
    """Forward HTTP request to BACKENDS[backend_id]. Inject API key. Strip hop headers."""
    if backend_id not in BACKENDS:
        return JSONResponse({"error": f"unknown backend '{backend_id}'"}, status_code=404)

    base_url, key, _ = BACKENDS[backend_id]
    target = f"{base_url}/{sub_path.lstrip('/')}"

    # Sanitize headers
    fwd_headers = {
        k: v for k, v in request.headers.items() if k.lower() not in _HOP_BY_HOP
    }
    # Inject API key (override client's, since we own auth)
    if key:
        fwd_headers["Authorization"] = f"Bearer {key}"
    else:
        fwd_headers.pop("authorization", None)

    body = await request.body()
    try:
        r = await _proxy_client.request(
            method=request.method,
            url=target,
            headers=fwd_headers,
            params=dict(request.query_params),
            content=body,
        )
    except httpx.TimeoutException:
        return JSONResponse({"error": "upstream timeout", "backend": backend_id}, status_code=504)
    except httpx.HTTPError as e:
        return JSONResponse({"error": f"upstream error: {e}", "backend": backend_id}, status_code=502)

    # Forward response. Pass-through content-type.
    out_headers = {
        k: v for k, v in r.headers.items() if k.lower() not in _HOP_BY_HOP
    }
    return JSONResponse(
        content=r.json() if "json" in r.headers.get("content-type", "") else {"raw": r.text},
        status_code=r.status_code,
        headers=out_headers,
    )


# Bound routes — explicit list so we control which backends are exposed
from fastapi import Request  # noqa: E402


@app.api_route("/actor/{sub_path:path}", methods=["GET", "POST", "PUT", "DELETE"])
async def proxy_actor(sub_path: str, request: Request):
    return await _forward("actor", sub_path, request)


@app.api_route("/actorvlm/{sub_path:path}", methods=["GET", "POST", "PUT", "DELETE"])
async def proxy_actorvlm(sub_path: str, request: Request):
    return await _forward("actorvlm", sub_path, request)
