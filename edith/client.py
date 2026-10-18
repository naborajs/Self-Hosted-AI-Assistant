"""HTTP client for communicating with EDITH FastAPI administrative endpoints in Project EDITH v2.0.

Provides programmatic access for the CLI and administrative scripts:
- Automatic JWT Bearer token authentication via `EDITH_ADMIN_TOKEN` or `authenticate()`.
- Synchronous HTTP requests wrapped with custom domain exception hierarchies.
- Structured abstractions for system overview, model configuration, user querying,
  memory operations, tool inspection, and queue management.
"""

from __future__ import annotations

import os
from typing import Any, Optional
import httpx


class EdithApiError(Exception):
    """Base exception for EDITH API client errors."""
    pass


class EdithApiConnectionError(EdithApiError):
    """Raised when the backend API is unreachable or connection fails."""
    pass


class EdithApiAuthError(EdithApiError):
    """Raised on authentication or authorization failure (401/403)."""
    pass


class EdithApiRequestError(EdithApiError):
    """Raised on bad request or server error (400, 404, 500, etc.)."""
    def __init__(self, message: str, status_code: int = 400, detail: Any = None):
        super().__init__(message)
        self.status_code = status_code
        self.detail = detail


class EdithApiClient:
    """Client for interacting with the EDITH FastAPI administrative API."""

    def __init__(
        self,
        base_url: str | None = None,
        token: str | None = None,
        timeout: float = 10.0,
    ) -> None:
        raw_url = base_url or os.getenv("EDITH_API_URL", "http://127.0.0.1:8000")
        self.base_url = raw_url.rstrip("/")
        self.token = token or os.getenv("EDITH_ADMIN_TOKEN")
        self.timeout = timeout

    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    def authenticate(self, username: str, password: str) -> str:
        """Authenticate with the backend and store the access token."""
        url = f"{self.base_url}/api/token"
        try:
            with httpx.Client(timeout=self.timeout) as client:
                resp = client.post(
                    url,
                    data={"username": username, "password": password},
                    headers={"Accept": "application/json"},
                )
        except httpx.RequestError as exc:
            raise EdithApiConnectionError(
                f"Failed to connect to EDITH backend at {self.base_url}: {exc}"
            ) from exc

        if resp.status_code == 401:
            raise EdithApiAuthError("Authentication failed: invalid administrator username or password.")
        if resp.status_code != 200:
            raise EdithApiRequestError(
                f"Authentication error ({resp.status_code}): {resp.text}",
                status_code=resp.status_code,
            )

        token_data = resp.json()
        self.token = token_data.get("access_token")
        return self.token

    def _request(
        self,
        method: str,
        path: str,
        params: dict[str, Any] | None = None,
        json_body: Any = None,
        requires_auth: bool = True,
    ) -> Any:
        url = f"{self.base_url}{path}"
        try:
            with httpx.Client(timeout=self.timeout) as client:
                resp = client.request(
                    method=method,
                    url=url,
                    params=params,
                    json=json_body,
                    headers=self._headers(),
                )
        except httpx.RequestError as exc:
            raise EdithApiConnectionError(
                f"EDITH backend is unreachable at {self.base_url}. Start services with start.bat or python main.py ({exc})"
            ) from exc

        if resp.status_code == 401:
            raise EdithApiAuthError(
                "Unauthorized: Authentication token is missing or expired. "
                "Provide a valid admin token via --token or run 'edith auth login'."
            )
        if resp.status_code == 403:
            raise EdithApiAuthError(
                "Forbidden: Administrator privileges required for this action."
            )
        if resp.status_code == 404:
            detail = "Resource not found"
            try:
                detail = resp.json().get("detail", detail)
            except Exception:
                pass
            raise EdithApiRequestError(detail, status_code=404)
        if resp.status_code >= 400:
            detail = resp.text
            try:
                detail = resp.json().get("detail", detail)
            except Exception:
                pass
            raise EdithApiRequestError(f"API Error ({resp.status_code}): {detail}", status_code=resp.status_code)

        try:
            return resp.json()
        except Exception:
            return resp.text

    def get_health(self) -> dict[str, Any]:
        """Check system health via public /health endpoint."""
        return self._request("GET", "/health", requires_auth=False)

    def get_overview(self) -> dict[str, Any]:
        """Get comprehensive admin overview."""
        return self._request("GET", "/api/admin/overview")

    def list_models(self, refresh: bool = False) -> dict[str, Any]:
        """List installed Ollama models."""
        path = "/api/models/refresh" if refresh else "/api/models"
        method = "POST" if refresh else "GET"
        return self._request(method, path)

    def get_model_config(self) -> dict[str, Any]:
        """Get model selection configuration and effective defaults."""
        return self._request("GET", "/api/models/config")

    def set_default_model(self, model_name: str | None) -> dict[str, Any]:
        """Set or clear the global default Ollama model."""
        return self._request("POST", "/api/models/config", json_body={"global_default_model": model_name})

    def list_users(self, limit: int = 50, offset: int = 0) -> dict[str, Any]:
        """List registered messaging users."""
        return self._request("GET", "/api/admin/users", params={"limit": limit, "offset": offset})

    def get_user(self, user_id: int) -> dict[str, Any]:
        """Get single user details."""
        return self._request("GET", f"/api/admin/users/{user_id}")

    def list_memory(self, user_id: int, query: str | None = None) -> dict[str, Any]:
        """List or search facts stored in long-term memory for a user."""
        params = {"target_user_id": user_id}
        if query:
            params["query"] = query
        return self._request("GET", "/api/admin/memory", params=params)

    def save_memory(self, user_id: int, key: str, value: str, tags: str | None = None) -> dict[str, Any]:
        """Save or update a fact in long-term memory for a user."""
        params = {
            "target_user_id": user_id,
            "key": key,
            "value": value,
            "action": "save",
        }
        if tags:
            params["tags"] = tags
        return self._request("POST", "/api/admin/memory", params=params)

    def delete_memory(self, user_id: int, key: str) -> dict[str, Any]:
        """Delete a fact from long-term memory for a user."""
        return self._request("POST", "/api/admin/memory", params={"target_user_id": user_id, "key": key, "action": "delete"})

    def clear_memory(self, user_id: int) -> dict[str, Any]:
        """Clear all facts from long-term memory for a user."""
        return self._request("POST", "/api/admin/memory", params={"target_user_id": user_id, "action": "clear"})

    def list_tools(self) -> dict[str, Any]:
        """Get registered tools catalog and execution policy."""
        return self._request("GET", "/api/admin/tools")

    def get_integrations(self) -> dict[str, Any]:
        """Get status of WhatsApp and Telegram integrations."""
        return self._request("GET", "/api/admin/integrations")

    def get_deliveries(
        self,
        status: str | None = None,
        platform: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        """Get diagnostic delivery queue items."""
        params = {"limit": limit, "offset": offset}
        if status:
            params["status"] = status
        if platform:
            params["platform"] = platform
        return self._request("GET", "/api/admin/diagnostics/deliveries", params=params)

    def retry_outbound(self, message_id: int) -> dict[str, Any]:
        """Safely requeue a failed or ambiguous outbound message."""
        return self._request("POST", f"/api/admin/diagnostics/outbound/{message_id}/retry")
