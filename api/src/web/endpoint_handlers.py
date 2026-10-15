"""Handlers for the proxy's simple HTTP endpoints."""

import contextlib
from typing import Any

from flask import Response, jsonify, request

from catalog.discovery import get_discovery_catalog
from catalog.openapi_spec import get_openapi_specification
from config import settings
from security.auth import AuthError, authenticate_caller, display_name_from_claims
from telemetry.logger import log_event


def health() -> Response:
    """Return service health and the advertised endpoints."""
    log_event("INFO", "Healthcheck requested", request=request, status_code=200)
    return jsonify(
        {
            "status": "healthy",
            "service": "copilot-chat-proxy",
            "runtime": "python-3.14",
            "allowed_regions": settings.allowed_regions,
            "endpoints": {
                "discovery": "/discovery",
                "openapi": "/openapi.json",
                "health": "/health",
            },
        },
    )


def discovery() -> Response:
    """Return the policy-filtered model discovery catalog."""
    user_email: str | None = None
    extra: dict[str, Any] = {}
    with contextlib.suppress(AuthError):
        user_email, claims = authenticate_caller(request.headers.get("Authorization"))
        if display_name := display_name_from_claims(claims):
            extra["principal_display_name"] = display_name
    catalog = get_discovery_catalog(user_email=user_email)
    allowed_models = [model["id"] for model in catalog.get("candidateModels", [])]
    extra["allowed_models"] = allowed_models
    log_event(
        "INFO",
        f"Discovery catalog requested by {user_email}: {allowed_models}" if user_email else f"Discovery catalog requested: {allowed_models}",
        request=request,
        user_email=user_email,
        status_code=200,
        extra=extra,
    )
    return jsonify(catalog)


def openapi() -> Response:
    """Return the OpenAPI specification of the proxy."""
    spec = get_openapi_specification()
    log_event(
        "INFO",
        "OpenAPI specification requested",
        request=request,
        status_code=200,
    )
    return jsonify(spec)
