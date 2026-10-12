"""Discovery engine for the proxy model catalog.

Downloads models.json from the configured models catalog URL on invocation,
filters models based on the enterprise allowlist, and caches the result in memory.
Compatible with schemas/models.schema.json from vertex-ai-models-chat-provider.
"""

import json
import re
import time
from copy import deepcopy
from dataclasses import dataclass
from typing import Any

import httpx

from config import settings

_CACHE_TTL_SECONDS: float = 300.0  # 5 minutes cache
_REQUEST_TIMEOUT_SECONDS: float = 5.0
_CATALOG_COMPONENT = re.compile(r"[a-zA-Z0-9_.@-]+")
_MAX_ROUTING_LENGTH = 256


@dataclass
class _CatalogCache:
    """In-memory copy of the last successfully downloaded catalog."""

    catalog: dict[str, Any] | None = None
    fetched_at: float = 0.0
    source_url: str | None = None


_cache = _CatalogCache()


class CatalogError(ValueError):
    """The upstream catalog cannot be safely cached or used as routing policy."""


def _validate_effort_policy(effort: Any) -> None:
    """Require unique named choices and a default belonging to those choices."""
    if not isinstance(effort, dict):
        raise CatalogError("Model effort must be an object")
    values = effort.get("values")
    if not isinstance(values, list) or not values or any(not isinstance(value, str) or not value.strip() or value != value.strip() for value in values):
        raise CatalogError("Model effort requires nonempty named choices")
    if len(values) != len(set(values)) or effort.get("default") not in values:
        raise CatalogError("Model effort requires unique choices and a default from those choices")


def _valid_routing_value(value: Any, *, namespaced: bool = False) -> bool:
    """Match the extension's literal routing schema without URL or dot-segment aliases."""
    if not isinstance(value, str) or len(value) > _MAX_ROUTING_LENGTH:
        return False
    components = value.split("/") if namespaced else [value]
    return all(component not in (".", "..") and _CATALOG_COMPONENT.fullmatch(component) is not None for component in components)


def _validate_catalog(catalog: Any) -> dict[str, Any]:
    """Validate routing, capacity and effort policy before accepting a catalog."""
    if not isinstance(catalog, dict) or not isinstance(catalog.get("candidateModels"), list):
        raise CatalogError("Catalog must contain a candidateModels array")
    regions = catalog.get("regionPriority")
    if not isinstance(regions, list) or any(not isinstance(region, str) or not region.strip() for region in regions):
        raise CatalogError("Catalog must contain a regionPriority string array")
    seen_ids = set()
    for model in catalog["candidateModels"]:
        if not isinstance(model, dict) or any(not _valid_routing_value(model.get(key), namespaced=key == "version") for key in ("id", "version", "vendor")):
            raise CatalogError("Catalog models require literal id, version and vendor strings")
        if model["id"] in seen_ids:
            raise CatalogError("Catalog contains duplicate model IDs")
        seen_ids.add(model["id"])
        capacity = model.get("maxInputTokens")
        if type(capacity) is not int or capacity <= 0:
            raise CatalogError("Catalog models require a positive maxInputTokens integer")
        if "effort" not in model:
            continue
        _validate_effort_policy(model["effort"])
    return catalog


def _download_catalog(catalog_url: str) -> dict[str, Any] | None:
    """Download and validate the catalog, returning None on any network or format failure."""
    try:
        response = httpx.get(
            catalog_url,
            headers={"User-Agent": "Google-Agent-Platform-for-Copilot-Chat-Proxy/1.0"},
            timeout=_REQUEST_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        data = _validate_catalog(json.loads(response.content.decode("utf-8-sig")))
    except httpx.HTTPError, ValueError:
        return None
    return data


def load_master_catalog() -> dict[str, Any]:
    """Return the master catalog from the configured URL, cached in memory and stale-tolerant."""
    now = time.time()
    catalog_url = settings.models_catalog_url
    if _cache.source_url != catalog_url:
        _cache.catalog, _cache.fetched_at, _cache.source_url = None, 0.0, catalog_url
    if _cache.catalog is not None and (now - _cache.fetched_at) < _CACHE_TTL_SECONDS:
        return _cache.catalog

    catalog = _download_catalog(catalog_url)
    if catalog is not None:
        _cache.catalog, _cache.fetched_at = catalog, now
    if _cache.catalog is not None:
        return _cache.catalog
    return {"candidateModels": [], "regionPriority": settings.allowed_regions}


def get_allowed_model_specs(catalog: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Select exact catalog IDs in policy order without authorizing aliases."""
    source = catalog if catalog is not None else load_master_catalog()
    candidates = {model["id"]: model for model in source["candidateModels"]}
    return [deepcopy(candidates[model_id]) for model_id in dict.fromkeys(settings.allowed_models) if model_id in candidates]


def get_discovery_catalog() -> dict[str, Any]:
    """Return the discovery response matching schemas/models.schema.json.

    - candidateModels: only models allowed by enterprise policy (settings.allowed_models)
    - regionPriority: settings.allowed_regions (e.g. ["global"]).
    """
    catalog = load_master_catalog()
    override_max_input = settings.discovery_max_input_tokens
    matched_models = get_allowed_model_specs(catalog)
    for model_entry in matched_models:
        if override_max_input > 0:
            model_entry["maxInputTokens"] = min(override_max_input, model_entry["maxInputTokens"])

    # Only regions granted by policy are exposed, keeping the catalog's priority order
    allowed_regions = settings.allowed_regions
    raw_regions = catalog.get("regionPriority", [])
    filtered_regions = [r for r in raw_regions if r.lower() in allowed_regions]
    if not filtered_regions:
        filtered_regions = allowed_regions

    return {"candidateModels": matched_models, "regionPriority": filtered_regions}
