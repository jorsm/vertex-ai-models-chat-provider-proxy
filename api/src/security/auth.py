"""Authentication and identity extraction module.

Validates Google OIDC ID tokens and extracts caller identity.
Access authorization is delegated to Google Cloud Run native IAM.
"""

import base64
import json
import time
from http import HTTPStatus
from typing import Any

import jwt
import requests

from config import settings

_GOOGLE_JWKS_CLIENT = jwt.PyJWKClient("https://www.googleapis.com/oauth2/v3/certs")
_ALLOWED_ISSUERS = ("accounts.google.com", "https://accounts.google.com")
_BEARER_PARTS = 2
_JWT_PARTS = 3
_TOKENINFO_TIMEOUT_SECONDS = 10


class AuthError(Exception):
    """Raised when the caller cannot be authenticated; carries the HTTP status to return."""

    def __init__(self, status_code: int, message: str) -> None:
        """Store the HTTP status code and the client-facing message."""
        self.status_code = status_code
        self.message = message
        super().__init__(message)


def display_name_from_claims(claims: dict[str, Any]) -> str | None:
    """Return the optional Google profile name from validated identity claims."""
    name = claims.get("name")
    if isinstance(name, str):
        return name.strip() or None
    return None


def extract_bearer_token(authorization_header: str | None) -> str:
    """Extract the Bearer token from the Authorization header."""
    if not authorization_header:
        raise AuthError(
            HTTPStatus.UNAUTHORIZED,
            "Missing Authorization header. Please authenticate with Google ADC.",
        )

    parts = authorization_header.strip().split(" ")
    if len(parts) != _BEARER_PARTS or parts[0].lower() != "bearer":
        raise AuthError(
            HTTPStatus.UNAUTHORIZED,
            "Invalid Authorization header format. Expected 'Bearer <token>'.",
        )

    return parts[1]


def _decode_verified_payload(payload_b64: str) -> dict[str, Any]:
    """Decode the payload of a token whose signature Cloud Run already verified at the edge."""
    try:
        padded = payload_b64 + "=" * (-len(payload_b64) % 4)
        return json.loads(base64.urlsafe_b64decode(padded).decode("utf-8"))
    except ValueError as exc:
        raise AuthError(
            HTTPStatus.UNAUTHORIZED,
            f"Failed to parse verified Cloud Run token payload: {exc}",
        ) from exc


def _verify_google_token(token: str) -> dict[str, Any]:
    """Validate an unmodified ID token via Google tokeninfo, falling back to JWKS."""
    try:
        response = requests.get(
            "https://oauth2.googleapis.com/tokeninfo",
            params={"id_token": token},
            timeout=_TOKENINFO_TIMEOUT_SECONDS,
        )
        if response.status_code == HTTPStatus.OK:
            return response.json()
        signing_key = _GOOGLE_JWKS_CLIENT.get_signing_key_from_jwt(token)
        return jwt.decode(
            token,
            signing_key.key,
            algorithms=[signing_key.algorithm_name],
            options={"verify_aud": False},
        )
    except (requests.RequestException, jwt.PyJWTError, ValueError) as exc:
        raise AuthError(
            HTTPStatus.UNAUTHORIZED,
            f"Google token verification failed: {exc!s}",
        ) from exc


def _validate_claims(claims: Any) -> str:
    """Check issuer, expiry, audience and email of the claims; return the normalized email."""
    if not claims or not isinstance(claims, dict):
        raise AuthError(HTTPStatus.UNAUTHORIZED, "Invalid token claims.")

    issuer = claims.get("iss")
    if issuer not in _ALLOWED_ISSUERS:
        raise AuthError(
            HTTPStatus.UNAUTHORIZED,
            f"Invalid token issuer: {issuer}. Expected Google accounts issuer.",
        )

    exp = claims.get("exp")
    if exp and time.time() > float(exp):
        raise AuthError(HTTPStatus.UNAUTHORIZED, "Google token has expired.")

    if settings.expected_audience and claims.get("aud") != settings.expected_audience:
        raise AuthError(
            HTTPStatus.UNAUTHORIZED,
            f"Invalid token audience. Expected {settings.expected_audience}",
        )

    email = claims.get("email")
    if not email:
        raise AuthError(
            HTTPStatus.UNAUTHORIZED,
            "Token is cryptographically valid but missing the 'email' claim.",
        )
    return email.lower().strip()


def authenticate_caller(authorization_header: str | None) -> tuple[str, dict[str, Any]]:
    """Validate the caller identity forwarded by Google Cloud Run.

    When Cloud Run requires authentication, Google edge infrastructure verifies the
    OIDC token signature and IAM role ('roles/run.invoker') before invoking the container.
    Cloud Run then forwards the payload with the signature replaced by '_REMOVED_BY_GOOGLE'.

    This function:
    1. Extracts the Bearer token.
    2. If token signature was stripped/verified by Cloud Run (ends with REMOVED_BY_GOOGLE),
       safely decodes the verified JWT payload.
    3. If token is unmodified, validates signature via Google tokeninfo or JWKS.
    4. Validates expiration (exp) and Google issuer (accounts.google.com). Access control
       is enforced natively by Cloud Run IAM (roles/run.invoker).

    Returns a tuple of (user_email, token_claims).
    Raises AuthError on invalid or expired token.
    """
    # Local development bypass (if enabled via LOCAL_DEV_BYPASS=true)
    if settings.local_dev_bypass:
        return (
            "local-developer@example.com",
            {"email": "local-developer@example.com", "sub": "local-dev"},
        )

    token = extract_bearer_token(authorization_header).strip()
    parts = token.split(".")
    if len(parts) == _JWT_PARTS and "REMOVED_BY_GOOGLE" in parts[2]:
        claims = _decode_verified_payload(parts[1])
    else:
        claims = _verify_google_token(token)

    return _validate_claims(claims), claims
