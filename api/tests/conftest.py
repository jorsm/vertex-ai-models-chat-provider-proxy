"""Pytest configuration and fixtures for cross-OS integration tests against the deployed proxy."""

import asyncio
import os
import shutil
import sys
from pathlib import Path

import pytest

# Ensure api/src is available on sys.path for all tests
SRC_DIR = str(Path(__file__).resolve().parent.parent / "src")
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)

GCLOUD_TOKEN_TIMEOUT_SECONDS = 15


async def _gcloud_identity_token(gcloud_bin: str) -> str:
    """Return the identity token printed by `gcloud auth print-identity-token`."""
    process = await asyncio.create_subprocess_exec(
        gcloud_bin,
        "auth",
        "print-identity-token",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=GCLOUD_TOKEN_TIMEOUT_SECONDS)
    if process.returncode != 0:
        raise RuntimeError(stderr.decode(errors="replace").strip())
    return stdout.decode().strip()


@pytest.fixture(scope="session")
def proxy_url() -> str:
    """Return the live URL of the deployed proxy, or skip the live tests when unset."""
    url = os.getenv("PROXY_SERVICE_URI", "").strip().rstrip("/")
    if not url:
        pytest.skip("Set PROXY_SERVICE_URI to the deployed proxy URL to run the live integration tests.")
    return url


@pytest.fixture(scope="session")
def vertex_project() -> str:
    """Return the Google Cloud project used in Vertex AI request paths, or skip when unset."""
    project = os.getenv("PROXY_PROJECT_ID", "").strip()
    if not project:
        pytest.skip("Set PROXY_PROJECT_ID to the proxy project ID to run the live integration tests.")
    return project


@pytest.fixture(scope="session")
def valid_auth_token() -> str:
    """Acquire a valid Google OIDC identity token in a cross-platform manner.

    Works on Windows, Linux, and macOS.
    """
    # 1. Environment variable override
    env_token = os.getenv("PROXY_AUTH_TOKEN") or os.getenv("GCP_ID_TOKEN")
    if env_token and env_token.strip():
        return env_token.strip()

    # 2. Extract from gcloud CLI
    gcloud_bin = shutil.which("gcloud") or shutil.which("gcloud.cmd")
    if gcloud_bin:
        try:
            token = asyncio.run(_gcloud_identity_token(gcloud_bin))
        except (OSError, RuntimeError, TimeoutError) as exc:
            pytest.skip(f"Unable to obtain OIDC token from gcloud: {exc}")
        if token:
            return token

    pytest.skip("No authentication token available. Run 'gcloud auth login' or set PROXY_AUTH_TOKEN.")


@pytest.fixture(scope="session")
def valid_headers(valid_auth_token: str) -> dict[str, str]:
    """Return standard HTTP headers with a valid Bearer token."""
    return {
        "Authorization": f"Bearer {valid_auth_token}",
        "Content-Type": "application/json",
    }


@pytest.fixture(autouse=True)
def disable_trace_network_export(monkeypatch: pytest.MonkeyPatch) -> None:
    """Local tests must explicitly opt into an in-memory trace provider."""
    monkeypatch.setenv("TRACE_ENABLED", "false")
    monkeypatch.setenv("MAX_CONTEXT_TOKENS", "500000")
    monkeypatch.setenv("DISCOVERY_MAX_INPUT_TOKENS", "250000")
