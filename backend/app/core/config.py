"""Application settings. Every secret lives here and nowhere else."""

from typing import Literal

from pydantic import computed_field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    APP_ENV: Literal["local", "staging", "production"] = "local"
    DEMO_MODE: bool = True

    # --- CORS -------------------------------------------------------------
    # The regex is what lets every Vercel preview deploy work without a
    # backend redeploy. Without it, only the exact origins below are allowed.
    # Kept as a raw string, not a list: pydantic-settings JSON-decodes complex
    # types straight out of dotenv, before any validator gets a chance to run,
    # so a plain comma-separated value would raise at import time.
    BACKEND_CORS_ORIGINS: str = "http://localhost:5173"
    BACKEND_CORS_ORIGIN_REGEX: str = r"https://.*\.vercel\.app"

    # --- Database ---------------------------------------------------------
    # DATABASE_URL wins when set: a direct asyncpg connection, used for local
    # development and integration tests. Otherwise we go through Supabase's
    # PostgREST. Both speak the same rpc() surface, so application code
    # never learns which one it is talking to.
    DATABASE_URL: str = ""

    # Supabase (server-side only; never shipped to a browser)
    SUPABASE_URL: str = ""
    SUPABASE_SERVICE_ROLE_KEY: str = ""

    # --- Merchant console -------------------------------------------------
    # Deliberately empty rather than a usable default. The old default was
    # "dev-merchant-key", a literal published in this repository -- so any
    # deploy that forgot the env var shipped with a key an attacker could read
    # off GitHub. Empty means require_merchant_key refuses every request,
    # which is a visible failure instead of a silent compromise.
    MERCHANT_API_KEY: str = ""

    # --- Razorpay ---------------------------------------------------------
    # KEY_SECRET signs the checkout callback; WEBHOOK_SECRET signs the
    # webhook body. They are different values -- mixing them up is a
    # signature failure that looks like a bug in your own code.
    RAZORPAY_KEY_ID: str = ""
    RAZORPAY_KEY_SECRET: str = ""
    RAZORPAY_WEBHOOK_SECRET: str = ""

    # Mint synthetic Razorpay orders and skip signature checks. Off unless
    # switched on deliberately, and ignored in production regardless: this
    # bypasses payment verification entirely, so it must never be reachable
    # by forgetting to set something.
    ALLOW_STUB_PAYMENTS: bool = False

    PUBLIC_APP_BASE_URL: str = "http://localhost:5173"

    # --- Agent-to-agent commerce -------------------------------------------
    # Base64 Ed25519 seed. Signs machine quotes so a buyer can verify a price
    # came from this shop without trusting the connection it arrived over.
    # Empty is supported: quotes are still served, marked signed=false. Serving
    # an unsigned quote that looked signed would be far worse than saying so.
    AGENT_SIGNING_SECRET_KEY: str = ""
    # Where a buyer should call back. Empty falls back to the request's own
    # base URL, which is right locally and wrong behind a proxy that rewrites
    # the host.
    PUBLIC_API_BASE_URL: str = ""

    # --- LLM providers ----------------------------------------------------
    LLM_PRIMARY: str = "ollama_cloud"
    LLM_FALLBACK: str = "groq"
    LLM_FORCE_PROVIDER: str = ""  # panic switch: pin one tier, skip the router

    OLLAMA_BASE_URL: str = "https://ollama.com/v1"
    OLLAMA_API_KEY: str = ""
    OLLAMA_MODEL: str = "gpt-oss:120b"

    GROQ_BASE_URL: str = "https://api.groq.com/openai/v1"
    GROQ_API_KEY: str = ""
    GROQ_MODEL: str = "openai/gpt-oss-20b"

    # The notebook used timeout=180. On a 90-second stage that is a hang,
    # not a timeout. These numbers are deliberately aggressive.
    LLM_CONNECT_TIMEOUT_S: float = 3.0
    LLM_READ_TIMEOUT_S: float = 12.0
    #: The advisor and the post-mortem are not negotiations. Nobody is standing
    #: at a counter waiting, and both send the whole catalogue and ask for
    #: structured JSON back -- which on a real shop takes well over the 12
    #: seconds a haggling turn is allowed. Holding them to the negotiation
    #: deadline is why "Suggest limits" returned "not reachable" every time
    #: while /health/llm reported the same provider healthy in 1.7s.
    LLM_ADVISOR_TIMEOUT_S: float = 60.0
    LLM_GLOBAL_DEADLINE_S: float = 25.0
    LLM_TEMPERATURE: float = 0.2
    AGENT_MAX_STEPS: int = 4

    # Circuit breaker: after N failures inside the window, skip the tier.
    LLM_BREAKER_FAILS: int = 2
    LLM_BREAKER_WINDOW_S: float = 60.0
    LLM_BREAKER_COOLDOWN_S: float = 120.0

    @computed_field
    @property
    def cors_origins(self) -> list[str]:
        return [o.strip().rstrip("/") for o in self.BACKEND_CORS_ORIGINS.split(",") if o.strip()]


settings = Settings()
