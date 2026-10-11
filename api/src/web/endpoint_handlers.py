"""Handlers for the proxy's simple HTTP endpoints."""

from flask import Response, jsonify, request

from catalog.discovery import get_discovery_catalog
from catalog.openapi_spec import get_openapi_specification
from config import settings
from telemetry.logger import log_event


def health() -> Response:
    """Return service health and the advertised endpoints."""
    log_event("INFO", "Healthcheck requested", request=request, status_code=200)
    return jsonify(
        {
            "status": "healthy",
            "service": "ai-companion-proxy",
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
    catalog = get_discovery_catalog()
    log_event("INFO", "Discovery catalog requested", request=request, status_code=200)
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
