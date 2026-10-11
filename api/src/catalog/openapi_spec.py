"""Load the OpenAPI 3.0.3 specification for the Google Agent Platform for Copilot Chat - Proxy.

Populate the static template with runtime settings.
"""

import json
from pathlib import Path
from typing import Any

from config import settings

_SOURCE_DIR = Path(__file__).resolve().parent
_TEMPLATE_CANDIDATES = (
    _SOURCE_DIR / "assets" / "openapi_template.json",
    _SOURCE_DIR.parent / "assets" / "openapi_template.json",
    _SOURCE_DIR.parent.parent / "assets" / "openapi_template.json",
)
_TEMPLATE_PATH = next(
    (path for path in _TEMPLATE_CANDIDATES if path.is_file()),
    _TEMPLATE_CANDIDATES[-1],
)


def get_openapi_specification() -> dict[str, Any]:
    """Build the OpenAPI specification from its template and runtime settings."""
    with _TEMPLATE_PATH.open(encoding="utf-8-sig") as template_file:
        spec = json.load(template_file)

    spec["servers"] = [
        {
            "url": settings.proxy_base_url,
            "description": "Cloud Run proxy service",
        },
    ]

    # Dynamic governance labels and models
    allowed_models = settings.allowed_models
    project_label = settings.project_label_key
    user_label = settings.user_label_key

    # Inject runtime config into schemas
    schemas = spec.get("components", {}).get("schemas", {})
    if "HealthResponse" in schemas:
        props = schemas["HealthResponse"].get("properties", {})
        if "allowed_regions" in props:
            props["allowed_regions"]["example"] = settings.allowed_regions

    if "DiscoveryResponse" in schemas:
        props = schemas["DiscoveryResponse"].get("properties", {})
        if "regionPriority" in props:
            props["regionPriority"]["example"] = settings.allowed_regions

    if "PredictRequest" in schemas:
        props = schemas["PredictRequest"].get("properties", {})
        if "model" in props:
            props["model"]["enum"] = allowed_models
        if "labels" in props:
            req_labels = []
            if settings.require_project_label:
                req_labels.append(project_label)
            if settings.require_user_label:
                req_labels.append(user_label)
            props["labels"]["required"] = req_labels
            props["labels"]["properties"] = {
                project_label: {
                    "type": "string",
                    "description": "Project tag for cost allocation",
                    "example": "ecommerce-frontend",
                },
                user_label: {
                    "type": "string",
                    "description": "Client username or developer alias",
                    "example": "jane_doe",
                },
            }

    return spec
