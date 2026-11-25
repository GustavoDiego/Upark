import re
import shutil
import sqlite3
from contextlib import closing
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import BinaryIO
from urllib.parse import urlsplit

from .profiles import UparkError

CHUNK_SIZE = 256 * 1024
MAX_BLOB_SIZE = 256 * 1024 * 1024
FILE_ROUTE = re.compile(r"/files/([a-f0-9]{32}/[A-Za-z0-9._-]{1,140})")


def prepare_database(filename: str) -> None:
    path = Path(filename)
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path, timeout=30)) as connection:
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS upark_files (
                id INTEGER PRIMARY KEY,
                object_key TEXT NOT NULL UNIQUE,
                data BLOB NOT NULL
            )
            """
        )


def store_blob(filename: str, key: str, source: BinaryIO, size: int) -> None:
    if size > MAX_BLOB_SIZE:
        raise UparkError("O perfil SQLite aceita arquivos de até 256 MiB.")
    prepare_database(filename)
    with closing(sqlite3.connect(filename, timeout=30)) as connection, connection:
        cursor = connection.execute(
            "INSERT INTO upark_files (object_key, data) VALUES (?, zeroblob(?))",
            (key, size),
        )
        written = 0
        with connection.blobopen("upark_files", "data", cursor.lastrowid) as blob:
            while chunk := source.read(CHUNK_SIZE):
                written += len(chunk)
                if written > size:
                    break
                blob.write(chunk)
        if written != size:
            raise UparkError("O arquivo mudou de tamanho durante o envio. Tente novamente.")


class DownloadHandler(BaseHTTPRequestHandler):
    def __init__(self, *args, database: str, **kwargs):
        self.database = Path(database).as_uri() + "?mode=ro"
        super().__init__(*args, **kwargs)

    def setup(self) -> None:
        self.request.settimeout(20)
        super().setup()

    def do_GET(self) -> None:
        self.send_download(include_content=True)

    def do_HEAD(self) -> None:
        self.send_download(include_content=False)

    def send_download(self, include_content: bool) -> None:
        try:
            match = FILE_ROUTE.fullmatch(urlsplit(self.path).path)
        except ValueError:
            match = None
        if not match:
            self.send_error(404)
            return

        key = match[1]
        response_started = False
        try:
            with closing(sqlite3.connect(self.database, uri=True)) as connection:
                row = connection.execute(
                    "SELECT id FROM upark_files WHERE object_key = ?", (key,)
                ).fetchone()
                if row is None:
                    self.send_error(404)
                    return
                with connection.blobopen("upark_files", "data", row[0], readonly=True) as blob:
                    self.send_response(200)
                    self.send_header("Content-Type", "application/octet-stream")
                    self.send_header("Content-Length", str(len(blob)))
                    self.send_header("Content-Disposition", f'attachment; filename="{key.split("/")[1]}"')
                    self.send_header("X-Content-Type-Options", "nosniff")
                    response_started = True
                    self.end_headers()
                    if include_content:
                        shutil.copyfileobj(blob, self.wfile, length=CHUNK_SIZE)
        except (ConnectionError, TimeoutError):
            # The recipient may close the connection before finishing a download.
            return
        except sqlite3.Error:
            if not response_started:
                self.send_error(503)

    def log_message(self, format, *args) -> None:
        pass


def serve(database: str, host: str, port: int) -> None:
    prepare_database(database)
    handler = partial(DownloadHandler, database=database)
    with ThreadingHTTPServer((host, port), handler) as server:
        print(f"SQLite atendendo em {host}:{server.server_port}. Ctrl+C para encerrar.", flush=True)
        server.serve_forever()
