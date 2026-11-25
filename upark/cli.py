import argparse
import getpass
import os
import re
import sqlite3
import stat
import sys
import warnings
from pathlib import Path

from .profiles import Profile, UparkError, config_dir, get_profile, profiles, save_profile
from .sqlite_storage import serve
from .storage import base_url, publish

PROVIDERS = {"1": "supabase", "2": "r2", "3": "s3", "4": "sqlite"}


def prompt(label: str, default: str = "", optional: bool = False) -> str:
    hint = f" [{default}]" if default else ""
    while True:
        value = input(f"{label}{hint}: ").strip() or default
        if value or optional:
            return value
        print("Preencha este campo.")


def secret(label: str, optional: bool = False) -> str:
    with warnings.catch_warnings():
        warnings.simplefilter("error", getpass.GetPassWarning)
        try:
            while True:
                value = getpass.getpass(f"{label}: ").strip()
                if value or optional:
                    return value
                print("Preencha este campo.")
        except getpass.GetPassWarning as error:
            raise UparkError("Abra o comando em um terminal que permita digitar a chave sem eco.") from error


def create() -> None:
    name = prompt("Nome do perfil")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,39}", name):
        raise UparkError("Use até 40 letras sem acento, números, hífens ou underscores no nome do perfil.")
    if any(existing.casefold() == name.casefold() for existing, _ in profiles()):
        raise UparkError(f"O perfil '{name}' já existe.")

    print("1. Supabase Storage\n2. Cloudflare R2\n3. Amazon S3 / compatível\n4. SQLite")
    choice = prompt("Plataforma", "1").lower()
    provider = PROVIDERS.get(choice, choice)
    if provider not in PROVIDERS.values():
        raise UparkError("Plataforma inválida. Escolha supabase, r2, s3 ou sqlite.")

    if provider == "sqlite":
        database = prompt("Arquivo SQLite", str(config_dir() / f"{name}.sqlite3"))
        database_path = Path(database).expanduser().resolve()
        if database_path == (config_dir() / "profiles.sqlite3").resolve():
            raise UparkError("Escolha um arquivo SQLite diferente do banco de perfis.")
        settings = {
            "database": str(database_path),
            "public_url": base_url(prompt("URL base de download", "http://127.0.0.1:8765")),
        }
    elif provider == "supabase":
        settings = {
            "project_url": base_url(prompt("URL do projeto Supabase")),
            "bucket": prompt("Nome do bucket público"),
            "api_key": secret("Chave secret do projeto (sb_secret_... ou service_role legado)"),
        }
    else:
        endpoint = prompt(
            "Endpoint S3 (URL da API do R2)" if provider == "r2" else "Endpoint S3 (Enter usa AWS)",
            optional=provider == "s3",
        )
        settings = {
            "endpoint": base_url(endpoint) if endpoint else "",
            "region": "auto" if provider == "r2" else prompt("Região", "us-east-1"),
            "bucket": prompt("Nome do bucket"),
            "public_url": base_url(prompt("URL base pública do bucket")),
            "access_key": secret("Access Key ID"),
            "secret_key": secret("Secret Access Key"),
            "session_token": secret("Token de sessão (Enter se não usa)", optional=True) if provider == "s3" else "",
        }
    save_profile(Profile(name, provider, settings))
    print(f"Perfil '{name}' cadastrado. Publique com upark add path=ARQUIVO use={name}.")


def add(filename: str, name: str) -> None:
    if not filename:
        raise UparkError("Informe um caminho em path=ARQUIVO.")
    path = Path(filename).expanduser()
    try:
        info = path.stat()
    except FileNotFoundError as error:
        raise UparkError(f"Arquivo não encontrado: {path}") from error
    if not stat.S_ISREG(info.st_mode):
        raise UparkError(f"O caminho precisa apontar para um arquivo: {path}")
    profile = get_profile(name)
    if path.resolve() == (config_dir() / "profiles.sqlite3").resolve():
        raise UparkError("O banco de perfis contém credenciais. Escolha outro arquivo.")
    if profile.provider == "sqlite" and path.resolve() == Path(profile.settings["database"]):
        raise UparkError("Escolha um arquivo diferente do banco SQLite de destino.")
    try:
        with path.open("rb") as source:
            size = os.fstat(source.fileno()).st_size
            print(publish(profile, path.name, source, size))
    except FileNotFoundError as error:
        raise UparkError(f"Arquivo não encontrado: {path}") from error


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args and args[0] in {"add", "serve"}:
        keys = ("path=", "use=") if args[0] == "add" else ("use=",)
        args[1:] = [f"--{arg}" if arg.startswith(keys) else arg for arg in args[1:]]

    parser = argparse.ArgumentParser(prog="upark", description="Publique arquivos usando seu armazenamento.")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("create", help="Cadastrar um perfil")
    commands.add_parser("list", help="Listar perfis cadastrados")
    add_command = commands.add_parser("add", help="Publicar um arquivo", usage="upark add path=ARQUIVO use=PERFIL")
    add_command.add_argument("--path", required=True, metavar="ARQUIVO")
    add_command.add_argument("--use", required=True, metavar="PERFIL")
    serve_command = commands.add_parser("serve", help="Servir os downloads de um perfil SQLite")
    serve_command.add_argument("--use", required=True, metavar="PERFIL")
    serve_command.add_argument("--host", default="127.0.0.1")
    serve_command.add_argument("--port", default=8765, type=int)
    parsed = parser.parse_args(args)

    try:
        if parsed.command == "create":
            create()
        elif parsed.command == "list":
            entries = profiles()
            for name, provider in entries:
                print(f"{name}\t{provider}")
            if not entries:
                print("Nenhum perfil cadastrado. Execute upark create.")
        elif parsed.command == "add":
            add(parsed.path, parsed.use)
        else:
            profile = get_profile(parsed.use)
            if profile.provider != "sqlite":
                raise UparkError("O comando serve requer um perfil SQLite.")
            if not 1 <= parsed.port <= 65535:
                raise UparkError("A porta precisa estar entre 1 e 65535.")
            serve(profile.settings["database"], parsed.host, parsed.port)
        return 0
    except KeyboardInterrupt:
        if parsed.command == "serve":
            return 0
        print("\nOperação cancelada.", file=sys.stderr)
        return 130
    except EOFError:
        print("Entrada encerrada antes de cadastrar o perfil.", file=sys.stderr)
        return 1
    except (UparkError, OSError, sqlite3.Error, ValueError) as error:
        print(f"Erro: {error}", file=sys.stderr)
        return 1
