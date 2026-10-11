"""Configuration settings for the Google Agent Platform for Copilot Chat - Proxy.

Require all governance settings from environment variables, with no defaults.
Target Python 3.14+.
"""

import json
import os


def _get_required_env(env_name: str) -> str:
    """Read a required environment variable or raise RuntimeError immediately."""
    val = os.getenv(env_name)
    if val is None or not val.strip():
        raise RuntimeError(f"Missing required configuration: environment variable '{env_name}' is not set.")
    return val.strip()


def _parse_required_list(env_name: str) -> list[str]:
    """Parse a required list from a comma-separated string or JSON array."""
    raw_val = _get_required_env(env_name)
    if raw_val.startswith("[") and raw_val.endswith("]"):
        try:
            parsed = json.loads(raw_val)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"Error parsing JSON array for '{env_name}': {exc}") from exc
        if isinstance(parsed, list):
            result = [str(item).strip() for item in parsed if str(item).strip()]
            if not result:
                raise RuntimeError(f"Invalid configuration: '{env_name}' is an empty array.")
            return result

    result = [item.strip() for item in raw_val.split(",") if item.strip()]
    if not result:
        raise RuntimeError(f"Invalid configuration: '{env_name}' contains no valid elements.")
    return result


def _parse_required_bool(env_name: str) -> bool:
    """Parse a required boolean from an environment variable ('true'/'false')."""
    raw_val = _get_required_env(env_name).lower()
    if raw_val in ("true", "1", "yes"):
        return True
    if raw_val in ("false", "0", "no"):
        return False
    raise RuntimeError(f"Invalid configuration: '{env_name}' must be 'true' or 'false', received: '{raw_val}'.")


class Settings:
    """Load application settings strictly from system environment variables.

    Fail immediately with RuntimeError if a mandatory setting is not configured.
    """

    @property
    def hub_project_id(self) -> str:
        """Project hosting the proxy itself (telemetry, and the default Vertex project)."""
        return _get_required_env("HUB_PROJECT_ID")

    @property
    def allowed_vertex_projects(self) -> list[str]:
        """Projects clients may target on Vertex AI, from optional `ALLOWED_VERTEX_PROJECTS`; the hub project when unset."""
        if not os.getenv("ALLOWED_VERTEX_PROJECTS", "").strip():
            return [self.hub_project_id]
        return _parse_required_list("ALLOWED_VERTEX_PROJECTS")

    @property
    def allowed_regions(self) -> list[str]:
        """Vertex AI regions enabled by policy from `ALLOWED_REGIONS`."""
        return _parse_required_list("ALLOWED_REGIONS")

    @property
    def chat_user_label_key(self) -> str:
        """Label key under which the chat client sends the user, from `CHAT_USER_LABEL_KEY`."""
        return _get_required_env("CHAT_USER_LABEL_KEY")

    @property
    def chat_project_label_key(self) -> str:
        """Label key under which the chat client sends the workspace, from `CHAT_PROJECT_LABEL_KEY`."""
        return _get_required_env("CHAT_PROJECT_LABEL_KEY")

    @property
    def billing_user_label_key(self) -> str:
        """Billing label key that carries the client-declared user, from `BILLING_USER_LABEL_KEY`."""
        return _get_required_env("BILLING_USER_LABEL_KEY")

    @property
    def billing_project_label_key(self) -> str:
        """Billing label key that carries the client-declared workspace, from `BILLING_PROJECT_LABEL_KEY`."""
        return _get_required_env("BILLING_PROJECT_LABEL_KEY")

    @property
    def billing_auth_user_label_key(self) -> str:
        """Billing label key that carries the verified caller, from `BILLING_AUTH_USER_LABEL_KEY`."""
        return _get_required_env("BILLING_AUTH_USER_LABEL_KEY")

    @property
    def require_project_label(self) -> bool:
        """Whether requests must include the project label, from `REQUIRE_PROJECT_LABEL`."""
        return _parse_required_bool("REQUIRE_PROJECT_LABEL")

    @property
    def require_user_label(self) -> bool:
        """Whether requests must include the user label, from `REQUIRE_USER_LABEL`."""
        return _parse_required_bool("REQUIRE_USER_LABEL")

    @property
    def allowed_models(self) -> list[str]:
        """Model IDs allowed by governance, from `ALLOWED_MODELS`."""
        return _parse_required_list("ALLOWED_MODELS")

    @property
    def models_catalog_url(self) -> str:
        """URL of the model metadata catalog, from `MODELS_CATALOG_URL`."""
        return _get_required_env("MODELS_CATALOG_URL")

    @property
    def proxy_base_url(self) -> str:
        """Public base URL used in generated OpenAPI, from `PROXY_BASE_URL`."""
        return _get_required_env("PROXY_BASE_URL").rstrip("/")

    @property
    def expected_audience(self) -> str:
        """Optional expected audience claim for verified ID tokens."""
        return os.getenv("EXPECTED_AUDIENCE", "").strip()

    @property
    def local_dev_bypass(self) -> bool:
        """Whether local development may bypass OIDC authentication."""
        return os.getenv("LOCAL_DEV_BYPASS", "false").lower() in ("true", "1")

    @property
    def log_execution_id(self) -> bool:
        """Whether request execution IDs are included in structured logs."""
        return os.getenv("LOG_EXECUTION_ID", "true").lower() in ("true", "1")

    @property
    def trace_enabled(self) -> bool:
        """Whether OpenTelemetry spans are exported, from `TRACE_ENABLED`."""
        return _parse_required_bool("TRACE_ENABLED")

    @property
    def trace_sampling_ratio(self) -> float:
        """Fraction of traces sampled for export, from `TRACE_SAMPLING_RATIO`."""
        ratio = float(_get_required_env("TRACE_SAMPLING_RATIO"))
        if not 0 <= ratio <= 1:
            raise RuntimeError("TRACE_SAMPLING_RATIO must be between 0 and 1")
        return ratio

    @property
    def trace_export_timeout_ms(self) -> int:
        """Maximum trace export wait in milliseconds, from `TRACE_EXPORT_TIMEOUT_MS`."""
        timeout = int(_get_required_env("TRACE_EXPORT_TIMEOUT_MS"))
        if timeout <= 0:
            raise RuntimeError("TRACE_EXPORT_TIMEOUT_MS must be positive")
        return timeout

    @property
    def max_context_tokens(self) -> int:
        """Maximum context window allowed for requests (e.g. 500000). Set <= 0 to disable."""
        val = _get_required_env("MAX_CONTEXT_TOKENS")
        try:
            return int(val)
        except ValueError as exc:
            raise RuntimeError(f"MAX_CONTEXT_TOKENS must be an integer, got '{val}'") from exc

    @property
    def discovery_max_input_tokens(self) -> int:
        """Maximum input tokens advertised in /discovery (e.g. 250000). Set <= 0 to keep upstream values."""
        val = _get_required_env("DISCOVERY_MAX_INPUT_TOKENS")
        try:
            return int(val)
        except ValueError as exc:
            raise RuntimeError(f"DISCOVERY_MAX_INPUT_TOKENS must be an integer, got '{val}'") from exc


settings = Settings()
