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
    cache_db_path: str = "./cache.db"

    model_config = {
        "env_file": ".env.local",
        "env_file_encoding": "utf-8",
    }


settings = Settings()  # type: ignore[call-arg]
