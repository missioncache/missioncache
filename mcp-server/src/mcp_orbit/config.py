"""Configuration for the orbit MCP server."""

from pathlib import Path

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Server configuration from environment variables."""

    # Path to the task database
    db_path: Path = Path.home() / ".claude" / "tasks.db"

    # Centralized orbit root directory
    orbit_root: Path = Path.home() / ".claude" / "orbit"

    # Active and completed subdirectory names
    active_dir_name: str = "active"
    completed_dir_name: str = "completed"

    # Dashboard base URL for out-of-band sync notifications (task creation).
    # Failures are silently ignored - dashboard is optional.
    dashboard_url: str = "http://localhost:8787"

    class Config:
        env_prefix = "ORBIT_"


settings = Settings()
