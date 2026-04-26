from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    gemini_api_key: str = ""
    api_bearer_token: str
    gemini_model: str = "gemini-2.5-flash"
    analyzer_provider: str = "gemini"  # "gemini" | "qwen"
    ollama_base_url: str = "http://localhost:11434"
    qwen_model: str = "qwen2.5-vl:7b"

    # Cache settings
    cache_enabled: bool = True

    # Redis connection. Required for keystore, rate limits, and cache.
    # Format: rediss://default:<password>@<host>:<port> for TLS,
    # or redis://localhost:6379 for local dev with redis-server.
    redis_url: str = "redis://localhost:6379"

    # TTL for cached analyses (seconds). 30 days = re-analyze stale data
    # naturally; also bounds Redis memory usage.
    analysis_cache_ttl_seconds: int = 30 * 24 * 60 * 60

    # Rate limit settings (per user, both must pass)
    rate_limit_per_minute: int = 10
    rate_limit_per_day: int = 100

    # Request body size cap (bytes). 32 KB is generous for any
    # legitimate request given we cap prompt at 2000 chars.
    max_body_bytes: int = 32 * 1024

    # Admin endpoint auth. Empty string disables /admin/* entirely
    # (the endpoints return 404). Set this in production to enable
    # remote key management.
    admin_token: str = ""

    model_config = {
        "env_file": ".env.local",
        "env_file_encoding": "utf-8",
    }


settings = Settings()  # type: ignore[call-arg]
