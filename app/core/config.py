"""Environment-driven configuration. Every tunable lives here; nothing is hard-coded in services."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal, Optional

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[2]

# Published Standard-tier prices (USD per 1M tokens) copied from
# https://ai.google.dev/gemini-api/docs/pricing on 2026-09-30. Thinking tokens are billed
# as output. Prices marked "through December 31, 2026" change on 2027-01-01; override
# with LLM_PRICE_INPUT_PER_MTOK / LLM_PRICE_OUTPUT_PER_MTOK when they do. A model that is
# absent here reports cost as unavailable (null) instead of a guessed number.
PRICING_SOURCE = "https://ai.google.dev/gemini-api/docs/pricing (retrieved 2026-09-30)"
MODEL_PRICING_USD_PER_MTOK: dict[str, tuple[float, float]] = {
    "gemini-3.6-flash": (0.75, 3.75),
    "gemini-3.7-flash": (0.75, 3.75),
    "gemini-3.8-flash": (0.75, 3.75),
    "gemini-3.5-flash": (1.50, 9.00),
    "gemini-3.5-flash-lite": (0.30, 2.50),
    "gemini-3.1-flash-lite": (0.25, 1.50),
    "gemini-2.5-flash": (0.30, 2.50),
    "gemini-2.5-flash-lite": (0.10, 0.40),
}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # ---- data ------------------------------------------------------------------
    # Unset → data/official when it holds deeplinks.json, otherwise data/dev_fixtures.
    data_dir: Optional[Path] = None
    artifacts_dir: Path = REPO_ROOT / "artifacts"

    # ---- LLM -------------------------------------------------------------------
    llm_provider: Literal["gemini", "fake", "none"] = "gemini"
    # Default chosen by measurement on this key (see metrics.md): best sustained availability,
    # quality within 0.02 step-accuracy points of gemini-3.6-flash, lowest price.
    llm_model: str = "gemini-3.1-flash-lite"
    # Tried in order when the primary model is overloaded/rate-limited/retired (503/429/404).
    llm_fallback_models: str = "gemini-3.5-flash-lite,gemini-3.6-flash"
    # Model for query variations; empty → llm_model.
    llm_enrichment_model: str = ""
    gemini_api_key: Optional[SecretStr] = None
    gemini_base_url: str = "https://generativelanguage.googleapis.com/v1beta"
    llm_timeout_s: float = 20.0
    llm_attempts_per_model: int = 2
    llm_repair_retries: int = 1
    llm_temperature: float = 0.0
    llm_seed: int = 7
    llm_thinking_level: str = "minimal"
    # Send a duplicate request when the first has not answered after this many seconds (0 = off).
    llm_hedge_after_s: float = 5.0
    # How long the cold path waits for LLM paraphrases after extraction finished (else templates).
    variations_grace_s: float = 0.3
    llm_max_output_tokens: int = 4096
    llm_price_input_per_mtok: Optional[float] = None
    llm_price_output_per_mtok: Optional[float] = None
    llm_startup_probe: bool = True

    # ---- embeddings ------------------------------------------------------------
    embedding_provider: Literal["fastembed", "hashing", "gemini"] = "fastembed"
    embedding_model: str = "BAAI/bge-small-en-v1.5"
    embedding_cache_dir: Optional[Path] = None
    hashing_dim: int = 768

    # ---- retrieval -------------------------------------------------------------
    bm25_k1: float = 1.2
    bm25_b: float = 0.75
    retrieval_top_k_bm25: int = 25
    retrieval_top_k_dense: int = 25
    retrieval_top_k_rerank: int = 12
    hybrid_w_bm25: float = 0.45
    hybrid_w_dense: float = 0.55
    # Target-screen resolver thresholds (see app/retrieval/reranker.py).
    deeplink_min_score: float = 0.50
    deeplink_min_margin: float = 0.03
    deeplink_mapper: Literal["hybrid", "rules", "llm", "bm25", "dense"] = "hybrid"

    # ---- cache -----------------------------------------------------------------
    cache_backend: Literal["sqlite", "memory"] = "sqlite"
    cache_path: Optional[Path] = None
    cache_prewarm_path: Optional[Path] = None
    cache_read_enabled: bool = True
    cache_write_enabled: bool = True
    semantic_cache_enabled: bool = True
    # Calibrated by scripts/calibrate_cache.py (see artifacts/reports/cache_calibration.json).
    semantic_cache_threshold: float = 0.76
    semantic_cache_threshold_no_concept: float = 0.93
    semantic_cache_margin: float = 0.0
    semantic_cache_top_k: int = 8
    # Variation keys must stay this close to the source query to be indexed.
    cache_key_min_similarity: float = 0.55

    # ---- SIIS knowledge retrieval (used when siis_response is omitted) ----------
    siis_retrieval_enabled: bool = True
    siis_retrieval_min_score: float = 0.62
    siis_retrieval_min_margin: float = 0.02

    # ---- request limits ----------------------------------------------------------
    max_query_chars: int = 2000
    max_siis_chars: int = 60000

    # ---- observability -------------------------------------------------------------
    log_level: str = "INFO"
    log_format: Literal["json", "text"] = "json"

    # ---- health ------------------------------------------------------------------
    health_require_llm: bool = True

    # ------------------------------------------------------------------------------
    @property
    def resolved_data_dir(self) -> Path:
        if self.data_dir is not None:
            return Path(self.data_dir)
        official = REPO_ROOT / "data" / "official"
        if (official / "deeplinks.json").exists():
            return official
        return REPO_ROOT / "data" / "dev_fixtures"

    @property
    def dataset_label(self) -> str:
        d = self.resolved_data_dir.resolve()
        if d == (REPO_ROOT / "data" / "official").resolve():
            return "official"
        if d == (REPO_ROOT / "data" / "dev_fixtures").resolve():
            return "dev_fixtures (synthetic, NOT OFFICIAL)"
        return f"custom:{d}"

    @property
    def resolved_cache_path(self) -> Path:
        return Path(self.cache_path) if self.cache_path else self.artifacts_dir / "cache" / "cache.sqlite"

    @property
    def resolved_prewarm_path(self) -> Path:
        return (
            Path(self.cache_prewarm_path)
            if self.cache_prewarm_path
            else self.artifacts_dir / "cache" / "prewarm_plans.jsonl"
        )

    @property
    def index_dir(self) -> Path:
        return self.artifacts_dir / "indexes"

    @property
    def fallback_models(self) -> list[str]:
        return [m.strip() for m in self.llm_fallback_models.split(",") if m.strip()]

    @property
    def enrichment_model(self) -> str:
        return self.llm_enrichment_model.strip() or self.llm_model

    def price_for(self, model: str) -> Optional[tuple[float, float]]:
        if self.llm_price_input_per_mtok is not None and self.llm_price_output_per_mtok is not None:
            return (self.llm_price_input_per_mtok, self.llm_price_output_per_mtok)
        return MODEL_PRICING_USD_PER_MTOK.get(model)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
