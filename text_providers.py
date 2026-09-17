#!/usr/bin/env python3
"""
text_providers.py

Config-driven text-generation backends for learner-facing enrichment.

Conceptual model::

    TextGenerationProvider
        ├── local     (Hugging Face runtime model, lazy-loaded)
        └── endpoint  (OpenAI-compatible API, e.g. local llama.cpp server)

Both modes render the SAME file-backed prompts (config/prompts/*.txt),
so the backend is replaceable without rewriting prompts in Python.

RULES:
  - HF model weights are RUNTIME assets, never reference/raw data.
    download.py must NOT fetch them. The local provider lets the
    Hugging Face library download missing files on first actual use
    (generate.py runtime) and reuses the HF cache afterwards.
  - Never install dependencies automatically. Missing packages fail
    with an exact `pip install ...` instruction.
  - Secrets (API keys) come from an environment variable named by
    config (api_key_env), never from config files or manifests.
"""

from __future__ import annotations

import json
import os
from typing import Any

import requests

HF_LOCAL_PROVIDER = "huggingface"
ENDPOINT_PROVIDER = "openai_compatible"

TEXT_MODES = ("local", "endpoint")

# Sensible small default for an 8 GB VRAM laptop (guidance only — any
# model_id remains configurable; nothing here enforces a memory limit).
DEFAULT_LOCAL_MODEL_ID = "Qwen/Qwen2.5-0.5B-Instruct"


class TextProviderError(RuntimeError):
    """Text backend misconfiguration or generation failure."""


class TextGenerationProvider:
    """Interface every text backend implements."""

    name = "base"

    def complete(self, *, system_prompt: str, user_prompt: str) -> str:
        """Return raw model text for the rendered prompts."""
        raise NotImplementedError

    def describe(self) -> dict:
        """Provenance for cache identity + manifests (no secrets/paths)."""
        raise NotImplementedError


class EndpointTextProvider(TextGenerationProvider):
    """OpenAI-compatible chat-completions endpoint (current behavior).

    Preserves the working llama.cpp-style call: POST
    {base_url}/v1/chat/completions (or {base_url}/chat/completions when
    base_url already ends with /v1), `response_format: json_object` with
    graceful retry when the server rejects it.
    """

    name = "endpoint"

    def __init__(self, *, provider: str = ENDPOINT_PROVIDER,
                 base_url: str = "", model: str = "",
                 timeout_seconds: int = 180, trust_env: bool = False,
                 api_key_env: str = "",
                 generation: dict | None = None):
        self.provider = (provider or ENDPOINT_PROVIDER).strip() or ENDPOINT_PROVIDER
        self.base_url = (base_url or "").rstrip("/")
        if not self.base_url:
            raise TextProviderError(
                "text.endpoint.base_url is empty. Set it in "
                "config/config.local.json (see config/config.example.json) "
                "or run generate.py --no-ai for debugging.")
        if not (model or "").strip():
            raise TextProviderError(
                "text.endpoint.model is empty. Set the model alias exposed "
                "by the server in config/config.local.json.")
        self.model = model.strip()
        try:
            self.timeout = int(timeout_seconds or 180)
        except (TypeError, ValueError):
            self.timeout = 180
        self.trust_env = bool(trust_env)
        self.api_key_env = (api_key_env or "").strip()
        self.generation = dict(generation or {})
        self.session = requests.Session()
        self.session.trust_env = self.trust_env

    @property
    def chat_url(self) -> str:
        if self.base_url.endswith("/v1"):
            return self.base_url + "/chat/completions"
        return self.base_url + "/v1/chat/completions"

    def _headers(self) -> dict[str, str]:
        if not self.api_key_env:
            return {}
        key = os.environ.get(self.api_key_env, "")
        if not key:
            # Named env var missing: proceed without auth (some local
            # servers need none) rather than failing; the server decides.
            return {}
        return {"Authorization": f"Bearer {key}"}

    def complete(self, *, system_prompt: str, user_prompt: str) -> str:
        temperature = self.generation.get("temperature", 0.2)
        max_tokens = self.generation.get("max_tokens", 1200)
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": temperature,
            "max_tokens": max_tokens,
            "response_format": {"type": "json_object"},
        }
        headers = self._headers()
        response = self.session.post(
            self.chat_url, json=payload, timeout=self.timeout,
            headers=headers or None)
        if response.status_code >= 400:
            # Some llama.cpp builds reject response_format: retry plain.
            payload.pop("response_format", None)
            response = self.session.post(
                self.chat_url, json=payload, timeout=self.timeout,
                headers=headers or None)
        response.raise_for_status()
        raw = response.json()
        return raw["choices"][0]["message"]["content"]

    def describe(self) -> dict:
        return {
            "mode": "endpoint",
            "provider": self.provider,
            # Model ALIAS only — never the endpoint URL, never a key.
            "model": self.model,
            "timeout_seconds": self.timeout,
            "trust_env": self.trust_env,
            "generation": dict(self.generation),
        }


class LocalHFTextProvider(TextGenerationProvider):
    """Hugging Face local text model (lazy-loaded, HF-cache-backed).

    First actual use downloads missing files automatically via
    `from_pretrained` into the normal Hugging Face cache
    (HF_HOME / ~/.cache/huggingface) and reuses them afterwards.
    Nothing here downloads eagerly: the model loads on the first
    complete() call inside generate.py.
    """

    name = "local"

    def __init__(self, *, provider: str = HF_LOCAL_PROVIDER,
                 model_id: str = "", revision: str = "",
                 device: str = "cuda", dtype: str = "auto",
                 generation: dict | None = None):
        self.provider = (provider or HF_LOCAL_PROVIDER).strip() or HF_LOCAL_PROVIDER
        model_id = (model_id or "").strip()
        if not model_id:
            raise TextProviderError(
                "text.local.model_id is empty. Set a Hugging Face model "
                "repository (e.g. \"Qwen/Qwen2.5-0.5B-Instruct\") in config.")
        self.model_id = model_id
        self.revision = (revision or "").strip()
        self.device = (device or "cuda").strip() or "cuda"
        self.dtype = (dtype or "auto").strip() or "auto"
        self.generation = dict(generation or {})
        self._pipe: Any = None

    def _require_deps(self) -> None:
        try:
            import transformers  # noqa: F401
        except ImportError as exc:
            raise TextProviderError(
                "Local text generation needs the `transformers` package "
                "(and a backend such as `torch`). Install with:\n"
                "    pip install transformers torch --index-url "
                "https://download.pytorch.org/whl/cu121\n"
                "then re-run generate.py (model files download automatically "
                "on first use).") from exc

    def _load(self) -> Any:
        if self._pipe is not None:
            return self._pipe
        self._require_deps()
        from transformers import AutoModelForCausalLM, AutoTokenizer
        try:
            import torch
        except ImportError as exc:
            raise TextProviderError(
                "Local text generation needs the `torch` package. "
                "Install with:\n"
                "    pip install torch --index-url "
                "https://download.pytorch.org/whl/cu121") from exc
        dtype_map = {
            "float16": torch.float16,
            "bfloat16": torch.bfloat16,
            "float32": torch.float32,
        }
        torch_dtype: Any = dtype_map.get(self.dtype.lower(), "auto")
        load_kwargs: dict[str, Any] = {}
        if self.revision:
            load_kwargs["revision"] = self.revision
        if torch_dtype != "auto":
            load_kwargs["torch_dtype"] = torch_dtype
        # from_pretrained downloads missing files on first use into the
        # standard HF cache and reuses them afterwards. No custom logic.
        tokenizer = AutoTokenizer.from_pretrained(
            self.model_id, trust_remote_code=False, **load_kwargs)
        model = AutoModelForCausalLM.from_pretrained(
            self.model_id, trust_remote_code=False, **load_kwargs)
        try:
            if self.device.lower() != "cpu":
                model = model.to(self.device)
        except Exception as exc:
            raise TextProviderError(
                f"could not place text model on device "
                f"{self.device!r}: {exc}. Set text.local.device to "
                f"\"cpu\" in config.") from exc
        model.eval()
        self._pipe = (tokenizer, model)
        return self._pipe

    def complete(self, *, system_prompt: str, user_prompt: str) -> str:
        tokenizer, model = self._load()
        try:
            import torch
        except ImportError as exc:
            raise TextProviderError(
                "Local text generation needs the `torch` package. "
                "Install with:\n"
                "    pip install torch --index-url "
                "https://download.pytorch.org/whl/cu121") from exc
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]
        try:
            chat_text = tokenizer.apply_chat_template(
                messages, tokenize=False, add_generation_prompt=True)
        except Exception:
            chat_text = (system_prompt + "\n\n" + user_prompt)
        inputs = tokenizer(chat_text, return_tensors="pt")
        try:
            inputs = {k: v.to(model.device) for k, v in inputs.items()}
        except Exception:
            pass
        gen_kwargs: dict[str, Any] = {
            "max_new_tokens": int(self.generation.get("max_tokens", 1200)),
            "do_sample": float(self.generation.get("temperature", 0.2)) > 0,
        }
        if gen_kwargs["do_sample"]:
            gen_kwargs["temperature"] = float(
                self.generation.get("temperature", 0.2))
        with torch.no_grad():
            output_ids = model.generate(**inputs, **gen_kwargs)
        input_len = inputs["input_ids"].shape[-1]
        new_ids = output_ids[0][input_len:]
        return tokenizer.decode(new_ids, skip_special_tokens=True)

    def describe(self) -> dict:
        return {
            "mode": "local",
            "provider": self.provider,
            "model_id": self.model_id,
            "revision": self.revision,
            "device": self.device,
            "dtype": self.dtype,
            "generation": dict(self.generation),
        }


def build_text_provider(text_cfg: dict) -> TextGenerationProvider:
    """Select a text backend from the generation.text config block."""
    cfg = dict(text_cfg or {})
    mode = str(cfg.get("mode", "endpoint") or "endpoint").strip().lower()
    if mode not in TEXT_MODES:
        raise TextProviderError(
            f"unknown generation.text.mode {mode!r} "
            f"(valid: {list(TEXT_MODES)})")
    if mode == "local":
        local = cfg.get("local", {})
        if not isinstance(local, dict):
            raise TextProviderError("generation.text.local must be an object")
        return LocalHFTextProvider(
            provider=str(local.get("provider", HF_LOCAL_PROVIDER)),
            model_id=str(local.get("model_id", "") or ""),
            revision=str(local.get("revision", "") or ""),
            device=str(local.get("device", "cuda") or "cuda"),
            dtype=str(local.get("dtype", "auto") or "auto"),
            generation=local.get("generation", {}),
        )
    endpoint = cfg.get("endpoint", {})
    if not isinstance(endpoint, dict):
        raise TextProviderError("generation.text.endpoint must be an object")
    return EndpointTextProvider(
        provider=str(endpoint.get("provider", ENDPOINT_PROVIDER)),
        base_url=str(endpoint.get("base_url", "") or ""),
        model=str(endpoint.get("model", "") or ""),
        timeout_seconds=endpoint.get("timeout_seconds", 180),
        trust_env=bool(endpoint.get("trust_env", False)),
        api_key_env=str(endpoint.get("api_key_env", "") or ""),
        generation=endpoint.get("generation", {}),
    )


def text_cache_identity(*, provider_desc: dict,
                        system_hash: str, user_hash: str,
                        profile_code: str, profile_instruction: str,
                        facts_canonical: str) -> str:
    """Deterministic text cache identity (any input change => new key)."""
    import hashlib
    canonical = json.dumps({
        "mode": provider_desc.get("mode", ""),
        "provider": provider_desc.get("provider", ""),
        # Local: model repository pins output. Endpoint: model alias.
        "model_id": provider_desc.get("model_id", ""),
        "revision": provider_desc.get("revision", ""),
        "endpoint_model": provider_desc.get("model", ""),
        "device": provider_desc.get("device", ""),
        "dtype": provider_desc.get("dtype", ""),
        "generation": provider_desc.get("generation", {}),
        "timeout_seconds": provider_desc.get("timeout_seconds", ""),
        "system_hash": system_hash,
        "user_hash": user_hash,
        "profile_code": profile_code,
        "profile_instruction": profile_instruction,
        "facts": facts_canonical,
    }, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
