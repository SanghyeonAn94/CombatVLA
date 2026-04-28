import os
import httpx
from fastapi import FastAPI, Query
from fastapi.responses import JSONResponse, PlainTextResponse

ACTORVLM_URL = os.environ.get("ACTORVLM_URL", "http://actorvlm:8000")
ACTORVLM_KEY = os.environ.get("ACTORVLM_KEY", "combatvla")
HOST_IP = os.environ.get("HOST_IP", "10.213.70.101")

app = FastAPI(title="ActorVLM Specs")

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
    return {"endpoints": ["/specs", "/specs?format=text", "/health"]}


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
