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

PROVIDERS = {"1": "supabase", "2": "r2", "3": "s3", "4": "sqlite", "5": "github"}


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


def assignments(entries: list[str], flag: str) -> dict[str, str]:
    result = {}
    for entry in entries:
        key, separator, value = entry.partition("=")
        if not separator or not re.fullmatch(r"[a-z][a-z0-9_]*", key) or key in result:
            raise UparkError(f"Use {flag} CAMPO=VALOR, uma vez por campo.")
        result[key] = value
    return result


def create(args: argparse.Namespace) -> None:
    automatic = args.name is not None or args.provider is not None or args.set or args.secret_env
    if automatic and (not args.name or not args.provider):
        raise UparkError("O cadastro sem prompts requer --name e --provider.")
    name = args.name if automatic else prompt("Nome do perfil")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,39}", name):
        raise UparkError("Use até 40 letras sem acento, números, hífens ou underscores no nome do perfil.")
    if any(existing.casefold() == name.casefold() for existing, _ in profiles()):
        raise UparkError(f"O perfil '{name}' já existe.")

    if not automatic:
        print("1. Supabase Storage\n2. Cloudflare R2\n3. Amazon S3 / compatível\n4. SQLite\n5. GitHub Releases")
    choice = (args.provider if automatic else prompt("Plataforma", "1")).lower()
    provider = PROVIDERS.get(choice, choice)
    if provider not in PROVIDERS.values():
        raise UparkError("Plataforma inválida. Escolha supabase, r2, s3, sqlite ou github.")

    values = assignments(args.set, "--set")
    references = assignments(args.secret_env, "--secret-env")
    environment = {}

    def value(key: str, label: str, default: str = "", optional: bool = False) -> str:
        if not automatic:
            return prompt(label, default, optional)
        configured = values.pop(key, default).strip()
        if not configured and not optional:
            raise UparkError(f"Informe --set {key}=VALOR.")
        return configured

    def credential(key: str, label: str, optional: bool = False) -> str:
        if not automatic:
            return secret(label, optional)
        variable = references.pop(key, None)
        if variable is None and optional:
            return ""
        if not variable or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", variable):
            raise UparkError(f"Informe --secret-env {key}=VARIAVEL com o nome de uma variável de ambiente.")
        environment[f"env:{key}"] = variable
        return ""

    if provider == "sqlite":
        database = value("database", "Arquivo SQLite", str(config_dir() / f"{name}.sqlite3"))
        database_path = Path(database).expanduser().resolve()
        if database_path == (config_dir() / "profiles.sqlite3").resolve():
            raise UparkError("Escolha um arquivo SQLite diferente do banco de perfis.")
        settings = {
            "database": str(database_path),
            "public_url": base_url(value("public_url", "URL base de download", "http://127.0.0.1:8765")),
        }
    elif provider == "supabase":
        settings = {
            "project_url": base_url(value("project_url", "URL do projeto Supabase")),
            "bucket": value("bucket", "Nome do bucket público"),
            "api_key": credential("api_key", "Chave secret do projeto (sb_secret_... ou service_role legado)"),
        }
    elif provider == "github":
        repository = value("repository", "Repositório público (dono/nome)")
        if not re.fullmatch(r"[A-Za-z0-9-]+/[A-Za-z0-9_.-]+", repository) or repository.endswith(("/.", "/..")):
            raise UparkError("Informe o repositório GitHub no formato DONO/REPOSITORIO.")
        tag = value("tag", "Tag da release publicada")
        settings = {
            "repository": repository,
            "tag": tag,
            "token": credential("token", "Token do GitHub com escrita em Contents"),
        }
    else:
        endpoint = value(
            "endpoint",
            "Endpoint S3 (URL da API do R2)" if provider == "r2" else "Endpoint S3 (Enter usa AWS)",
            optional=provider == "s3",
        )
        settings = {
            "endpoint": base_url(endpoint) if endpoint else "",
            "region": "auto" if provider == "r2" else value("region", "Região", "us-east-1"),
            "bucket": value("bucket", "Nome do bucket"),
            "public_url": base_url(value("public_url", "URL base pública do bucket")),
            "access_key": credential("access_key", "Access Key ID"),
            "secret_key": credential("secret_key", "Secret Access Key"),
            "session_token": credential("session_token", "Token de sessão (Enter se não usa)", optional=True) if provider == "s3" else "",
        }
    if values or references:
        raise UparkError("Há campos incompatíveis com a plataforma ou com a opção de cadastro. Veja upark create --help.")
    save_profile(Profile(name, provider, settings | environment))
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
    create_command = commands.add_parser("create", help="Cadastrar um perfil")
    create_command.add_argument("--name", help="Nome do perfil para cadastro sem prompts")
    create_command.add_argument("--provider", help="supabase, r2, s3, sqlite ou github")
    create_command.add_argument("--set", action="append", default=[], metavar="CAMPO=VALOR", help="Configuração não secreta, opção repetível")
    create_command.add_argument("--secret-env", action="append", default=[], metavar="CAMPO=VARIAVEL", help="Credencial lida de variável de ambiente no envio, opção repetível")
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
            create(parsed)
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
