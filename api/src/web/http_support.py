"""Shared HTTP behavior: structured routing errors."""

from flask import Flask, Response, jsonify, request

from telemetry.logger import log_event


def handle_not_found(_error: Exception) -> tuple[Response, int]:
    """Return the structured 404 payload and log it."""
    error = {
        "error": {
            "code": 404,
            "status": "NOT_FOUND",
            "message": (f"Endpoint '{request.path}' not found. Available endpoints: /discovery, /openapi.json, /health, /v1/projects/..."),
        },
    }
    log_event(
        "WARNING",
        f"Endpoint not found: {request.path}",
        request=request,
        status_code=404,
        error_response=error,
    )
    return jsonify(error), 404


def handle_method_not_allowed(_error: Exception) -> tuple[Response, int]:
    """Return the structured 405 payload and log it."""
    error = {
        "error": {
            "code": 405,
            "status": "METHOD_NOT_ALLOWED",
            "message": f"HTTP method '{request.method}' is not allowed for '{request.path}'.",
        },
    }
    log_event(
        "WARNING",
        "Method not allowed",
        request=request,
        status_code=405,
        error_response=error,
    )
    return jsonify(error), 405


def register_http_support(app: Flask) -> None:
    """Register error handling on the Flask app."""
    app.register_error_handler(404, handle_not_found)
    app.register_error_handler(405, handle_method_not_allowed)
