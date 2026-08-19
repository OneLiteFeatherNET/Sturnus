"""Process configuration from the environment.

Runtime settings that administrators change live in the database (see
`ConfigStore`); this module holds only what must exist before the bot can
reach a database at all.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="STURNUS_", frozen=True)

    discord_token: SecretStr
    database_url: str
    s3_endpoint: str
    s3_bucket: str
    s3_access_key: SecretStr
    s3_secret_key: SecretStr
    # Base64-encoded 32 bytes. See the encryption task for how it is used.
    master_key: SecretStr
    # Names which master key encrypted a given recording, so the key can be
    # rotated without re-encrypting existing material.
    master_key_id: str
    recording_dir: Path
    health_port: int = 8080


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
