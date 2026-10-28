from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ROOT / ".env", env_file_encoding="utf-8", extra="ignore")

    gemini_api_key: str = ""
    gemini_models: str = ""
    tavily_api_key: str = ""
    jina_api_key: str = ""
    composio_api_key: str = ""
    supabase_url: str = ""
    supabase_service_key: str = ""

    data_dir: Path = ROOT / "data"

    max_pages: int = 4
    max_chars_per_page: int = 12000
    http_timeout_s: float = 20.0
    llm_timeout_s: float = 120.0
    llm_max_output_tokens: int = 12000
    total_timeout_s: float = 170.0

    rate_limit_per_ip: int = 4
    rate_limit_window_s: int = 3600
    daily_generation_cap: int = 60
    max_concurrent_generations: int = 2
    trust_proxy: bool = False

    @property
    def models_list(self) -> list[str]:
        out: list[str] = []
        for m in self.gemini_models.split(","):
            m = m.strip().removeprefix("models/")
            if m and m not in out:
                out.append(m)
        return out

    @property
    def seed_dir(self) -> Path:
        return self.data_dir / "guides"

    @property
    def live_dir(self) -> Path:
        return self.data_dir / "live"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
