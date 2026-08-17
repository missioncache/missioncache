"""Configuration for the MissionCache MCP server."""

from pathlib import Path

import missioncache_db
from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Server configuration from environment variables."""

    # Both path defaults come from missioncache_db, the single owner, rather
    # than re-deriving `Path.home() / ".missioncache"` here. Two independent
    # literals is what let MISSIONCACHE_ROOT move `root` while leaving db_path
    # behind. default_factory so the value is read when Settings is built, not
    # when this module is imported, which is the same call-time rule the
    # ownership block in missioncache_db states for every other consumer.
    db_path: Path = Field(default_factory=lambda: missioncache_db.DB_PATH)
    root: Path = Field(default_factory=lambda: missioncache_db.MISSIONCACHE_ROOT)

    # Active and completed subdirectory names
    active_dir_name: str = "active"
    completed_dir_name: str = "completed"

    # Dashboard base URL for out-of-band sync notifications (task creation).
    # Failures are silently ignored - dashboard is optional.
    dashboard_url: str = "http://localhost:8787"

    # env_ignore_empty because a set-but-empty value is never what the caller
    # meant, and pydantic would otherwise treat it as an explicit choice:
    # MISSIONCACHE_ROOT="" resolved to Path("."), the server's working
    # directory, and MISSIONCACHE_ACTIVE_DIR_NAME="" collapsed
    # root/active/<name> to root/<name>. It applies to every field, which is
    # why it beats a per-field guard here.
    #
    # A relative MISSIONCACHE_ROOT is handled at the owner rather than here:
    # missioncache_db refuses anything non-absolute, because the consumers do
    # not share a working directory and a relative value would mean a different
    # directory per process. Doing it there rather than in this class is what
    # keeps the MCP server and the dashboard reading one env value one way.
    model_config = SettingsConfigDict(
        env_prefix="MISSIONCACHE_", env_ignore_empty=True
    )

    @model_validator(mode="after")
    def _db_path_follows_root(self):
        """Keep the DB inside the root unless the DB path was set explicitly.

        Without this the two fields drift: setting MISSIONCACHE_ROOT moved
        `root` (pydantic maps the env var onto it) while `db_path` kept its own
        default, so one process wrote project files under the override and DB
        rows into the real home. `model_fields_set` excludes defaults but does
        include env-sourced values, so MISSIONCACHE_DB_PATH is still honored.

        Fires once per instance, and that is what makes `settings.root = X` on
        the singleton safe: assigning here marks `db_path` as set, so the guard
        is already false by the time any later assignment happens. Fixtures that
        set `root` then `db_path` are unaffected. It also means adding
        validate_assignment=True would NOT make db_path follow a reassigned
        root, so do not reach for it expecting that.
        """
        if "db_path" not in self.model_fields_set:
            self.db_path = self.root / "tasks.db"
        return self


settings = Settings()
