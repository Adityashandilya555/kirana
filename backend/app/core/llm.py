"""Provider router.

Ported from ~/agent1/movie_recommendation_agent.ipynb, which proved that
Ollama Cloud speaks the OpenAI wire format well enough for LangChain's
ChatOpenAI to drive it -- including tool calling.

Three things the notebook did that must NOT survive into production:
  * timeout=180      -> a hang, not a timeout, on a 90-second stage
  * temperature=0.4  -> too loose for a component that names prices
  * no failover      -> Ollama Cloud has no SLA and no status page

Tier 1 Ollama Cloud, tier 2 Groq, tier 3 (in agent.py) a deterministic
non-LLM responder so the demo degrades instead of hanging.

IMPORTANT: use bind_tools, never with_structured_output. Ollama Cloud
silently ignores JSON-Schema structured outputs (ollama/ollama#12362),
so schema forcing returns plausible prose instead of raising.
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field

import httpx
from langchain_openai import ChatOpenAI

from app.core.config import settings


@dataclass(frozen=True)
class ProviderSpec:
    name: str
    tier: int
    base_url: str
    api_key: str
    model: str
    reasoning_effort: str | None = None

    @property
    def configured(self) -> bool:
        return bool(self.api_key)


def _specs() -> dict[str, ProviderSpec]:
    return {
        "ollama_cloud": ProviderSpec(
            name="ollama_cloud",
            tier=1,
            base_url=settings.OLLAMA_BASE_URL,
            api_key=settings.OLLAMA_API_KEY,
            model=settings.OLLAMA_MODEL,
            # gpt-oss is a thinking model; unsuppressed it burns seconds of
            # hidden tokens on every turn.
            reasoning_effort="none",
        ),
        "groq": ProviderSpec(
            name="groq",
            tier=2,
            base_url=settings.GROQ_BASE_URL,
            api_key=settings.GROQ_API_KEY,
            model=settings.GROQ_MODEL,
        ),
    }


# --------------------------------------------------------------------------
# Circuit breaker. Ollama Cloud's documented failure mode is intermittent
# 503s that retrying does not help (ollama/ollama#15419), so once a tier
# starts flapping we stop paying its latency and go straight to the next.
# --------------------------------------------------------------------------
@dataclass
class _Breaker:
    fails: deque[float] = field(default_factory=deque)
    open_until: float = 0.0


_breakers: dict[str, _Breaker] = {}


def _breaker(name: str) -> _Breaker:
    return _breakers.setdefault(name, _Breaker())


def record_failure(name: str) -> None:
    b, now = _breaker(name), time.monotonic()
    b.fails.append(now)
    while b.fails and now - b.fails[0] > settings.LLM_BREAKER_WINDOW_S:
        b.fails.popleft()
    if len(b.fails) >= settings.LLM_BREAKER_FAILS:
        b.open_until = now + settings.LLM_BREAKER_COOLDOWN_S
        b.fails.clear()


def record_success(name: str) -> None:
    b = _breaker(name)
    b.fails.clear()
    b.open_until = 0.0


def is_open(name: str) -> bool:
    return time.monotonic() < _breaker(name).open_until


def reset_breakers() -> None:
    _breakers.clear()


# --------------------------------------------------------------------------
# Model construction
# --------------------------------------------------------------------------
def build_chat_model(spec: ProviderSpec) -> ChatOpenAI:
    """One ChatOpenAI per provider. Only base_url/api_key/model differ --
    which is exactly the notebook's PROVIDER switch, generalised."""
    kwargs: dict = {
        "model": spec.model,
        "base_url": spec.base_url,
        "api_key": spec.api_key,
        "temperature": settings.LLM_TEMPERATURE,
        "max_retries": 0,  # the router handles failover; retrying here doubles latency
        "timeout": httpx.Timeout(
            settings.LLM_READ_TIMEOUT_S, connect=settings.LLM_CONNECT_TIMEOUT_S
        ),
    }
    if spec.reasoning_effort:
        kwargs["reasoning_effort"] = spec.reasoning_effort
    return ChatOpenAI(**kwargs)


def provider_chain() -> list[ProviderSpec]:
    """Providers to attempt, best first. Honours the panic switch, skips
    unconfigured providers, and skips any tier whose breaker is open."""
    specs = _specs()

    forced = settings.LLM_FORCE_PROVIDER.strip()
    if forced:
        spec = specs.get(forced)
        return [spec] if spec and spec.configured else []

    ordered = [settings.LLM_PRIMARY, settings.LLM_FALLBACK]
    chain = []
    for name in ordered:
        spec = specs.get(name)
        if spec and spec.configured and spec.name not in {s.name for s in chain}:
            chain.append(spec)
    live = [s for s in chain if not is_open(s.name)]
    # If every breaker is open, try anyway rather than skipping straight to
    # the deterministic tier -- a cooldown is a hint, not a verdict.
    return live or chain


async def ping(spec: ProviderSpec) -> dict:
    """Cheap reachability probe for GET /health/llm."""
    if not spec.configured:
        return {"name": spec.name, "tier": spec.tier, "reachable": False,
                "latency_ms": None, "error": "no api key configured"}
    started = time.monotonic()
    try:
        llm = build_chat_model(spec)
        reply = await llm.ainvoke("Reply with the single word: ready")
        return {
            "name": spec.name, "tier": spec.tier, "reachable": True,
            "latency_ms": int((time.monotonic() - started) * 1000),
            "model": spec.model,
            "sample": (reply.content or "")[:40],
            "breaker_open": is_open(spec.name),
            "error": None,
        }
    except Exception as exc:  # noqa: BLE001 - a probe must never raise
        return {
            "name": spec.name, "tier": spec.tier, "reachable": False,
            "latency_ms": int((time.monotonic() - started) * 1000),
            "model": spec.model,
            "breaker_open": is_open(spec.name),
            "error": f"{type(exc).__name__}: {exc}"[:300],
        }


async def health_report() -> dict:
    specs = _specs()
    results = [await ping(s) for s in specs.values()]
    return {
        "primary": settings.LLM_PRIMARY,
        "fallback": settings.LLM_FALLBACK,
        "forced": settings.LLM_FORCE_PROVIDER or None,
        "chain": [s.name for s in provider_chain()],
        "providers": results,
    }
