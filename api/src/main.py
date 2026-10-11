"""WSGI application for the AI Companion proxy (Cloud Run / Gunicorn entry point `main:app`)."""

import os

from flask import Flask, Response, request

from inference.handler import handle_inference
from web.endpoint_handlers import discovery, health, openapi
from web.http_support import register_http_support

LOCAL_PORT = 8080

app = Flask(__name__)
register_http_support(app)

app.add_url_rule("/health", view_func=health, methods=["GET"], strict_slashes=False)
app.add_url_rule("/discovery", view_func=discovery, methods=["GET"], strict_slashes=False)
app.add_url_rule("/openapi.json", view_func=openapi, methods=["GET"], strict_slashes=False)


@app.post("/v1/projects/<path:_resource>")
def inference(_resource: str) -> Response:
    """Forward an authenticated Vertex AI inference request through the proxy."""
    return handle_inference(request)


if __name__ == "__main__":
    os.environ.setdefault("LOCAL_DEV_BYPASS", "true")
    app.run(host="127.0.0.1", port=LOCAL_PORT)
