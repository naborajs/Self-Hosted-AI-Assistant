"""FastAPI REST API and security subsystem for Project EDITH v2.0.

This package provides the HTTP interface for administrative control and machine integration:
- `routes.py`: REST endpoints for machine chat (`/api/v1/chat`), administrative observability
  (`/api/admin/*`), model discovery (`/api/models/*`), and bridge status.
- `security.py`: Cryptographic primitives for bcrypt password validation, HS256 JWT bearer
  tokens, and timing-safe SHA-256 machine client API key authentication.
- `middleware.py`: Per-client sliding window rate limiting.
"""

from .routes import api_router, v1_router

__all__ = ["api_router", "v1_router"]
