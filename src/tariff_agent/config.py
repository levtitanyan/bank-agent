"""Configuration: environment settings plus the YAML files under ``config/``.

Two kinds of configuration, deliberately kept apart:

* **Secrets and environment** (:class:`Settings`) come from ``.env`` / real
  environment variables. Nothing secret is ever committed; ``.env.example``
  documents the names with empty or placeholder values.
* **Policy and domain data** (:func:`load_allowlist`, :func:`load_products`)
  live in version-controlled YAML, because a reviewer must be able to see in the
  repository exactly which domains are reachable and which products exist.

Everything is parsed into pydantic models at load time, so a typo in YAML fails
immediately with a clear message instead of surfacing as an odd behaviour
halfway through a monitoring run.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any, Final

import yaml
from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict

from tariff_agent.errors import ConfigError

PROJECT_ROOT: Final[Path] = Path(__file__).resolve().parents[2]
"""Repository root, derived from this file's location (src/tariff_agent/config.py)."""

CONFIG_DIR: Final[Path] = PROJECT_ROOT / "config"
"""Directory holding the version-controlled YAML policy files."""


class Settings(BaseSettings):
    """Environment-provided settings.

    Application settings use the ``TARIFF_`` prefix. ``GOOGLE_API_KEY`` and
    ``GOOGLE_GENAI_USE_VERTEXAI`` are intentionally unprefixed: the google-genai
    SDK reads those exact names itself, so renaming them would break the SDK.

    Attributes:
        gemini_model: Model id used for extraction (env ``TARIFF_GEMINI_MODEL``).
        log_level: Root log level (env ``TARIFF_LOG_LEVEL``).
        google_api_key: AI Studio key (env ``GOOGLE_API_KEY``). ``None`` when
            unset, which is valid: offline demos and tests run without a key.
    """

    model_config = SettingsConfigDict(
        env_prefix="TARIFF_",
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    gemini_model: str = "gemini-2.5-flash-lite"
    log_level: str = "INFO"
    google_api_key: SecretStr | None = Field(default=None, alias="GOOGLE_API_KEY")

    @property
    def has_api_key(self) -> bool:
        """Whether a Gemini API key is configured.

        Returns:
            True if a non-empty key is present. Callers use this to choose the
            offline mock path instead of failing.
        """
        return self.google_api_key is not None and bool(self.google_api_key.get_secret_value())


class Allowlist(BaseModel):
    """Domains and URL schemes the agent may fetch.

    Pure data: the matching logic lives in the Phase 2 URL policy, which applies
    it to every request *and every redirect hop*.

    Attributes:
        allowed_schemes: Permitted URL schemes, normally ``("https",)`` only.
        domains: Exact hostnames. No wildcards, so subdomains are not implied.
    """

    model_config = ConfigDict(frozen=True)

    allowed_schemes: tuple[str, ...] = Field(min_length=1)
    domains: tuple[str, ...] = Field(min_length=1)


class Product(BaseModel):
    """One monitored banking product and how users might refer to it.

    Attributes:
        id: Stable product identifier used in snapshots and diffs.
        kind: Product family, e.g. ``"loan"``. Selects the tariff schema later.
        name_hy: Official Armenian product name.
        name_en: English product name.
        synonyms: Language code -> alternative names, used by fuzzy resolution.
        seed_pages: Known product pages, used when live discovery finds nothing.
        seed_documents: Known official PDFs, same fallback role.
    """

    model_config = ConfigDict(frozen=True)

    id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    kind: str
    name_hy: str
    name_en: str
    synonyms: dict[str, tuple[str, ...]] = Field(default_factory=dict)
    seed_pages: tuple[str, ...] = ()
    seed_documents: tuple[str, ...] = ()

    @property
    def all_names(self) -> tuple[str, ...]:
        """Official names plus every synonym, for fuzzy matching."""
        names = [self.name_hy, self.name_en]
        for variants in self.synonyms.values():
            names.extend(variants)
        return tuple(names)


class ProductCatalog(BaseModel):
    """All monitored products of one bank.

    Attributes:
        bank: Bank name, copied into every extraction and snapshot.
        products: The monitored products, in configuration order.
    """

    model_config = ConfigDict(frozen=True)

    bank: str
    products: tuple[Product, ...] = Field(min_length=1)

    def get(self, product_id: str) -> Product | None:
        """Look up a product by id.

        Args:
            product_id: Product identifier, e.g. ``"mortgage"``.

        Returns:
            The product, or ``None`` if no such id is configured.
        """
        return next((p for p in self.products if p.id == product_id), None)

    @property
    def product_ids(self) -> tuple[str, ...]:
        """Ids of all configured products, in configuration order."""
        return tuple(p.id for p in self.products)


def _read_yaml(path: Path) -> dict[str, Any]:
    """Read a YAML mapping from disk.

    Args:
        path: File to read.

    Returns:
        The parsed top-level mapping.

    Raises:
        ConfigError: If the file is missing, malformed, or not a mapping.
    """
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigError(f"configuration file not found: {path}") from exc
    except yaml.YAMLError as exc:
        raise ConfigError(f"invalid YAML in {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError(f"expected a YAML mapping at the top level of {path}")
    return raw


def load_allowlist(path: Path | None = None) -> Allowlist:
    """Load the domain allowlist.

    Args:
        path: Override for the YAML location; defaults to ``config/allowlist.yaml``.

    Returns:
        The parsed :class:`Allowlist`.

    Raises:
        ConfigError: If the file is missing or does not match the schema.
    """
    path = path or CONFIG_DIR / "allowlist.yaml"
    try:
        return Allowlist.model_validate(_read_yaml(path))
    except ValidationError as exc:
        raise ConfigError(f"invalid allowlist configuration in {path}: {exc}") from exc


def load_products(path: Path | None = None) -> ProductCatalog:
    """Load the product catalog.

    Args:
        path: Override for the YAML location; defaults to ``config/products.yaml``.

    Returns:
        The parsed :class:`ProductCatalog`.

    Raises:
        ConfigError: If the file is missing, invalid, or has duplicate product ids.
    """
    path = path or CONFIG_DIR / "products.yaml"
    try:
        catalog = ProductCatalog.model_validate(_read_yaml(path))
    except ValidationError as exc:
        raise ConfigError(f"invalid product configuration in {path}: {exc}") from exc
    ids = catalog.product_ids
    if len(set(ids)) != len(ids):
        raise ConfigError(f"duplicate product ids in {path}: {ids}")
    return catalog


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings, loaded once.

    Returns:
        The :class:`Settings` instance.

    Raises:
        ConfigError: If the environment does not satisfy the schema.
    """
    try:
        return Settings()
    except ValidationError as exc:
        raise ConfigError(f"invalid environment configuration: {exc}") from exc
