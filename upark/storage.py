import http.client
import re
import unicodedata
from typing import BinaryIO
from urllib.error import HTTPError
from urllib.parse import quote, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener, urlopen
from uuid import uuid4

from .profiles import Profile, UparkError
from .sqlite_storage import store_blob

DOWNLOAD_TYPE = "application/octet-stream"
HTTP_ERRORS = (OSError, http.client.HTTPException)


def base_url(value: str) -> str:
    try:
        parsed = urlsplit(value)
        parsed.port
    except ValueError as error:
        raise UparkError("Informe uma URL HTTP válida.") from error
    loopback = parsed.hostname in {"localhost", "127.0.0.1", "::1"}
    allowed_scheme = parsed.scheme == "https" or (parsed.scheme == "http" and loopback)
    if (
        not allowed_scheme
        or not parsed.hostname
        or parsed.username is not None
        or parsed.query
        or parsed.fragment
        or any(ord(char) < 32 or char.isspace() or char in "?#\\" for char in value)
    ):
        raise UparkError("Use uma URL HTTPS, ou HTTP em localhost, sem credenciais, query ou fragmento.")
    return value.rstrip("/")


def object_key(filename: str) -> str:
    ascii_name = unicodedata.normalize("NFKD", filename).encode("ascii", "ignore").decode()
    safe_name = re.sub(r"[^A-Za-z0-9._-]+", "-", ascii_name).strip(".-") or "arquivo"
    if len(safe_name) > 140:
        stem, dot, extension = safe_name.rpartition(".")
        safe_name = stem[:120] + dot + extension if dot and len(extension) <= 19 else safe_name[:140]
    return f"{uuid4().hex}/{safe_name}"


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def upload_supabase(settings: dict[str, str], key: str, source: BinaryIO, size: int) -> str:
    root = f"{settings['project_url']}/storage/v1/object"
    bucket = quote(settings["bucket"], safe="")
    api_key = settings["api_key"]
    request = Request(
        f"{root}/{bucket}/{key}",
        data=source,
        headers={
            "apikey": api_key,
            "Authorization": f"Bearer {api_key}",
            "Content-Type": DOWNLOAD_TYPE,
            "Content-Length": str(size),
            "x-upsert": "false",
        },
        method="POST",
    )
    try:
        with build_opener(NoRedirect()).open(request, timeout=60):
            pass
    except HTTPError as error:
        error.close()
        raise UparkError(f"Supabase recusou o envio (HTTP {error.code}). Confira a chave, o bucket e o limite do arquivo.") from error
    except HTTP_ERRORS as error:
        raise UparkError("Falha na conexão de upload com Supabase.") from error
    return f"{root}/public/{bucket}/{key}?download"


def upload_s3(settings: dict[str, str], key: str, source: BinaryIO) -> str:
    try:
        import boto3
        from boto3.exceptions import S3UploadFailedError
        from boto3.s3.transfer import TransferConfig
        from botocore.config import Config
        from botocore.exceptions import BotoCoreError, ClientError
    except ImportError as error:
        raise UparkError('Para usar S3 ou R2, instale com python -m pip install ".[s3]".') from error

    try:
        with boto3.client(
            "s3",
            endpoint_url=settings["endpoint"] or None,
            region_name=settings["region"],
            aws_access_key_id=settings["access_key"],
            aws_secret_access_key=settings["secret_key"],
            aws_session_token=settings["session_token"] or None,
            config=Config(
                signature_version="s3v4",
                request_checksum_calculation="when_required",
                response_checksum_validation="when_required",
                s3={"addressing_style": "path" if settings["endpoint"] else "auto"},
            ),
        ) as client:
            client.upload_fileobj(
                source,
                settings["bucket"],
                key,
                ExtraArgs={
                    "ContentType": DOWNLOAD_TYPE,
                    "ContentDisposition": "attachment",
                },
                Config=TransferConfig(use_threads=False),
            )
    except (BotoCoreError, ClientError, S3UploadFailedError) as error:
        detail = ""
        if isinstance(error, ClientError):
            status = error.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
            if isinstance(status, int):
                detail = f" (HTTP {status})"
        raise UparkError(f"Falha no envio para S3{detail}. Confira as credenciais, a região e o bucket.") from error
    return f"{settings['public_url']}/{key}"


def publish(profile: Profile, filename: str, source: BinaryIO, size: int) -> str:
    key = object_key(filename)
    settings = profile.settings
    if profile.provider == "sqlite":
        store_blob(settings["database"], key, source, size)
        return f"{settings['public_url']}/files/{key}"
    if profile.provider == "supabase":
        link = upload_supabase(settings, key, source, size)
    else:
        link = upload_s3(settings, key, source)

    # Make an anonymous request. The returned URL must work for the recipient.
    headers = {"Range": "bytes=0-0"} if size else {}
    try:
        with urlopen(Request(link, headers=headers), timeout=20):
            return link
    except HTTPError as error:
        error.close()
        detail = f"HTTP {error.code}"
    except HTTP_ERRORS:
        detail = "sem resposta"
    raise UparkError(
        f"Arquivo enviado como '{key}', mas o acesso público falhou ({detail}). "
        "Confira a configuração pública do bucket. "
        f"URL esperada: {link}"
    )
