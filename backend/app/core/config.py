"""Application settings. Every secret lives here and nowhere else."""

from typing import Annotated, Any, Literal

from pydantic import BeforeValidator, computed_field
from pydantic_settings import BaseSettings, SettingsConfigDict


def _split_csv(v: Any) -> Any:
    """Accept either a comma-separated string or a real list from the env."""
    if isinstance(v, str) and not v.startswith("["):
        return [item.strip() for item in v.split(",") if item.strip()]
    return v


CommaSeparated = Annotated[list[str], BeforeValidator(_split_csv)]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    APP_ENV: Literal["local", "staging", "production"] = "local"
    DEMO_MODE: bool = True

    # --- CORS -------------------------------------------------------------
    # The regex is what lets every Vercel preview deploy work without a
    # backend redeploy. Without it, only the exact origins below are allowed.
    BACKEND_CORS_ORIGINS: CommaSeparated = ["http://localhost:5173"]
    BACKEND_CORS_ORIGIN_REGEX: str = r"https://.*\.vercel\.app"

    # --- Supabase (server-side only; never shipped to a browser) ----------
    SUPABASE_URL: str = ""
    SUPABASE_SERVICE_ROLE_KEY: str = ""

    # --- Merchant console -------------------------------------------------
    MERCHANT_API_KEY: str = "dev-merchant-key"

    # --- Razorpay ---------------------------------------------------------
    # KEY_SECRET signs the checkout callback; WEBHOOK_SECRET signs the
    # webhook body. They are different values -- mixing them up is a
    # signature failure that looks like a bug in your own code.
    RAZORPAY_KEY_ID: str = ""
    RAZORPAY_KEY_SECRET: str = ""
    RAZORPAY_WEBHOOK_SECRET: str = ""

    PUBLIC_APP_BASE_URL: str = "http://localhost:5173"

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
        return [str(o).rstrip("/") for o in self.BACKEND_CORS_ORIGINS]


settings = Settings()
