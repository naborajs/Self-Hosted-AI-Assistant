"""EDITH operator terminal CLI and administrative client suite for Project EDITH v2.0.

This package provides a professional command-line interface for the system owner:
- `cli.py`: Click-based operator command group (`status`, `doctor`, `models`, `users`, `memory`, `tools`, `queue`, `integrations`).
- `client.py`: HTTP API client for authenticated communication with the local FastAPI daemon.
- `formatter.py`: Rich-based terminal visual formatting with color-coded tables, status panels, and raw JSON modes.
"""

__version__ = "0.1.0"
__author__ = "Local AI Team"
