import json
import os
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path


class UparkError(Exception):
    pass


@dataclass(frozen=True)
class Profile:
    name: str
    provider: str
    settings: dict[str, str]


def config_dir() -> Path:
    override = os.environ.get("UPARK_HOME")
    if override:
        return Path(override).expanduser().resolve()
    if os.name == "nt":
        root = os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local"
    else:
        root = os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config"
    return Path(root) / "upark"


def open_profiles() -> sqlite3.Connection:
    directory = config_dir()
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    filename = directory / "profiles.sqlite3"
    os.close(os.open(filename, os.O_CREAT | os.O_WRONLY, 0o600))
    connection = sqlite3.connect(filename)
    try:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS profiles (
                name TEXT PRIMARY KEY COLLATE NOCASE,
                provider TEXT NOT NULL,
                settings TEXT NOT NULL
            )
            """
        )
    except BaseException:
        connection.close()
        raise
    return connection


def profiles() -> list[tuple[str, str]]:
    with closing(open_profiles()) as connection:
        return connection.execute(
            "SELECT name, provider FROM profiles ORDER BY name"
        ).fetchall()


def get_profile(name: str) -> Profile:
    with closing(open_profiles()) as connection:
        row = connection.execute(
            "SELECT name, provider, settings FROM profiles WHERE name = ?", (name,)
        ).fetchone()
        if row is None:
            names = connection.execute("SELECT name FROM profiles ORDER BY name")
            available = ", ".join(item[0] for item in names)
            raise UparkError(
                f"Perfil '{name}' não existe. Perfis cadastrados: "
                f"{available or 'nenhum (execute upark create)'}."
            )
    return Profile(row[0], row[1], json.loads(row[2]))


def save_profile(profile: Profile) -> None:
    with closing(open_profiles()) as connection, connection:
        cursor = connection.execute(
            "INSERT INTO profiles VALUES (?, ?, ?) ON CONFLICT(name) DO NOTHING",
            (profile.name, profile.provider, json.dumps(profile.settings)),
        )
        if cursor.rowcount == 0:
            raise UparkError(f"O perfil '{profile.name}' já existe.")
