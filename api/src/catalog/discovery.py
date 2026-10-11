"""Discovery engine for the proxy model catalog.

Downloads models.json from the configured models catalog URL on invocation,
filters models based on the enterprise allowlist, and caches the result in memory.
Compatible with schemas/models.schema.json from vertex-ai-models-chat-provider.
"""

import json
import time
from dataclasses import dataclass
from typing import Any

import httpx

from config import settings

_CACHE_TTL_SECONDS: float = 300.0  # 5 minutes cache
_REQUEST_TIMEOUT_SECONDS: float = 5.0


@dataclass
class _CatalogCache:
    """In-memory copy of the last successfully downloaded catalog."""

    catalog: dict[str, Any] | None = None
    fetched_at: float = 0.0


_cache = _CatalogCache()


def _download_catalog(catalog_url: str) -> dict[str, Any] | None:
    """Download and validate the catalog, returning None on any network or format failure."""
    try:
        response = httpx.get(
            catalog_url,
            headers={"User-Agent": "Google-Agent-Platform-for-Copilot-Chat-Proxy/1.0"},
            timeout=_REQUEST_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        data = json.loads(response.content.decode("utf-8-sig"))
    except httpx.HTTPError, ValueError:
        return None
    return data if isinstance(data, dict) and "candidateModels" in data else None


def load_master_catalog() -> dict[str, Any]:
    """Return the master catalog from the configured URL, cached in memory and stale-tolerant."""
    now = time.time()
    if _cache.catalog is not None and (now - _cache.fetched_at) < _CACHE_TTL_SECONDS:
        return _cache.catalog

    catalog = _download_catalog(settings.models_catalog_url)
    if catalog is not None:
        _cache.catalog, _cache.fetched_at = catalog, now
    if _cache.catalog is not None:
        return _cache.catalog
    return {"candidateModels": [], "regionPriority": settings.allowed_regions}


def get_discovery_catalog() -> dict[str, Any]:
    """Return the discovery response matching schemas/models.schema.json.

    - candidateModels: only models allowed by enterprise policy (settings.allowed_models)
    - regionPriority: settings.allowed_regions (e.g. ["global"]).
    """
    catalog = load_master_catalog()
    raw_candidates = catalog.get("candidateModels", [])
    allowed_list = settings.allowed_models
    override_max_input = settings.discovery_max_input_tokens

    matched_models: list[dict[str, Any]] = []
    seen_ids = set()

    # Only explicit allowlist entries are exposed; effort variants are separate models.
    for allowed in allowed_list:
        if allowed in seen_ids:
            continue
        candidate = next((m for m in raw_candidates if m.get("id") == allowed), None)
        if candidate is None:
            # Preserve the dot/hyphen alias compatibility without matching prefixes.
            normalized = allowed.replace(".", "-")
            candidate = next(
                (m for m in raw_candidates if m.get("id", "").replace(".", "-") == normalized),
                None,
            )
            if candidate is not None:
                candidate = {**candidate, "id": allowed}
        if candidate is not None:
            model_entry = dict(candidate)
            if override_max_input > 0:
                model_entry["maxInputTokens"] = override_max_input
            matched_models.append(model_entry)
            seen_ids.add(allowed)

    # Only regions granted by policy are exposed, keeping the catalog's priority order
    allowed_regions = settings.allowed_regions
    raw_regions = catalog.get("regionPriority", [])
    filtered_regions = [r for r in raw_regions if r.lower() in allowed_regions]
    if not filtered_regions:
        filtered_regions = allowed_regions

    return {"candidateModels": matched_models, "regionPriority": filtered_regions}


def get_model_spec(model_id: str) -> dict[str, Any] | None:
    """Return model metadata and pricing from the discovery source.

    Model aliases may use dots or hyphens interchangeably and policy profiles
    may append ``-high``, ``-medium`` or ``-max``. Exact matches always win.
    """
    candidates = load_master_catalog().get("candidateModels", [])
    if not isinstance(candidates, list):
        return None

    requested = model_id.replace(".", "-")
    for candidate in candidates:
        if str(candidate.get("id", "")).replace(".", "-") == requested:
            return candidate

    policy_suffixes = ("-high", "-medium", "-max")
    base_requested = requested
    for suffix in policy_suffixes:
        if base_requested.endswith(suffix):
            base_requested = base_requested[: -len(suffix)]
            break

    for candidate in candidates:
        candidate_id = str(candidate.get("id", "")).replace(".", "-")
        candidate_base = candidate_id
        for suffix in policy_suffixes:
            if candidate_base.endswith(suffix):
                candidate_base = candidate_base[: -len(suffix)]
                break
        if candidate_base == base_requested:
            return candidate

    return None
