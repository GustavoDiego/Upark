import io
import json
import os
import re
import sqlite3
import tempfile
import unittest
from contextlib import closing, contextmanager, redirect_stderr, redirect_stdout
from functools import partial
from http.client import HTTPResponse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.util import find_spec
from pathlib import Path
from threading import Thread
from unittest.mock import Mock, patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from upark.cli import main
from upark.profiles import UparkError
from upark.sqlite_storage import DownloadHandler, prepare_database, store_blob
from upark.storage import NoRedirect


@contextmanager
def http_server(handler, **state):
    with ThreadingHTTPServer(("127.0.0.1", 0), handler) as server:
        server.__dict__.update(state)
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield server, f"http://127.0.0.1:{server.server_port}"
        finally:
            server.shutdown()
            thread.join()


class StorageFixture(BaseHTTPRequestHandler):
    def do_PUT(self):
        self.server.upload_path = self.path
        self.server.upload_headers = dict(self.headers)
        self.server.payload = self.rfile.read(int(self.headers["Content-Length"]))
        self.send_response(200)
        self.end_headers()

    def do_GET(self):
        self.server.download_path = self.path
        self.server.download_headers = dict(self.headers)
        self.send_response(200)
        self.end_headers()
        self.wfile.write(self.server.payload)

    def log_message(self, format, *args):
        pass


class MemorySocket:
    def __init__(self, request: bytes):
        self.request = request
        self.response = bytearray()

    def settimeout(self, timeout):
        pass

    def makefile(self, mode, buffering=None):
        return io.BytesIO(self.request)

    def sendall(self, data):
        self.response.extend(data)


def local_download(handler, path, method="GET"):
    from urllib.parse import urlsplit

    route = urlsplit(path).path
    connection = MemorySocket(f"{method} {route} HTTP/1.0\r\n\r\n".encode())
    handler(connection, ("127.0.0.1", 0), None)
    response = HTTPResponse(MemorySocket(connection.response), method=method)
    response.begin()
    return response


class UparkTests(unittest.TestCase):
    def setUp(self):
        folder = self.enterContext(tempfile.TemporaryDirectory())
        self.directory = Path(folder)
        self.enterContext(patch.dict(os.environ, {"UPARK_HOME": folder, "NO_PROXY": "127.0.0.1"}))
        self.source = self.directory / "Olá mundo.pdf"
        self.payload = bytes(range(256)) * 3000
        self.source.write_bytes(self.payload)

    def cli(self, *args):
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = main(list(args))
        return code, stdout.getvalue().strip(), stderr.getvalue().strip()

    def create(self, fields, keys=()):
        with patch("builtins.input", side_effect=fields), patch("getpass.getpass", side_effect=keys):
            code, output, error = self.cli("create")
        self.assertEqual(code, 0, error)
        self.assertIn("cadastrado", output)
        for key in keys:
            if key:
                self.assertNotIn(key, output)

    def test_missing_path_unknown_profile_and_duplicate(self):
        self.create(["local", "4", "", ""])
        self.assertEqual(self.cli("list"), (0, "local\tsqlite", ""))
        with patch("builtins.input", return_value="LOCAL"):
            self.assertIn("já existe", self.cli("create")[2])
        code, output, error = self.cli("add", "path=ausente.pdf", "use=local")
        self.assertEqual((code, output), (1, ""))
        self.assertIn("Arquivo não encontrado", error)
        self.assertIn("apontar para um arquivo", self.cli("add", f"path={self.directory}", "use=local")[2])
        code, output, error = self.cli("add", f"path={self.source}", "use=ausente")
        self.assertEqual((code, output), (1, ""))
        self.assertIn("Perfil 'ausente' não existe", error)
        self.assertIn("cadastrados: local", error)

    def test_sqlite_downloads_and_keeps_same_names_separate(self):
        database = str(self.directory / "armazém.sqlite3")
        prepare_database(database)
        handler = partial(DownloadHandler, database=database)
        self.create(["local", "4", database, ""])
        code, first, error = self.cli("add", f"path={self.source}", "use=LOCAL")
        self.assertEqual(code, 0, error)
        second = self.cli("add", "--path", str(self.source), "--use", "local")[1]
        self.assertNotEqual(first, second)
        self.assertRegex(first, r"/files/[a-f0-9]{32}/Ola-mundo.pdf$")
        with local_download(handler, first) as response:
            self.assertEqual(response.status, 200)
            self.assertEqual(response.read(), self.payload)
            self.assertIn("attachment", response.headers["Content-Disposition"])
        with local_download(handler, second, "HEAD") as response:
            self.assertEqual(int(response.headers["Content-Length"]), len(self.payload))
            self.assertEqual(response.read(), b"")
        self.source.write_bytes(b"")
        code, empty, error = self.cli("add", f"path={self.source}", "use=local")
        self.assertEqual(code, 0, error)
        with local_download(handler, empty) as response:
            self.assertEqual(response.status, 200)
            self.assertEqual(response.read(), b"")
        with local_download(handler, f"/files/{'0' * 32}/arquivo") as response:
            self.assertEqual(response.status, 404)

    def test_sqlite_rolls_back_partial_blob(self):
        filename = str(self.directory / "storage.sqlite3")
        with self.assertRaises(UparkError):
            store_blob(filename, "test", io.BytesIO(b"a"), 4)
        with closing(sqlite3.connect(filename)) as connection:
            count = connection.execute("SELECT count(*) FROM upark_files").fetchone()[0]
        self.assertEqual(count, 0)

    def test_supabase_upload_and_anonymous_download(self):
        endpoint = "https://example.supabase.co"
        self.create(["cloud", "1", endpoint, "public"], ["sb_secret_test"])

        def upload(request, timeout):
            self.assertEqual(request.get_method(), "POST")
            self.assertRegex(request.full_url, r"/storage/v1/object/public/[a-f0-9]{32}/Ola-mundo.pdf$")
            self.assertEqual(request.get_header("Apikey"), "sb_secret_test")
            self.assertEqual(request.data.read(), self.payload)
            return io.BytesIO()

        with patch("upark.storage.build_opener", return_value=Mock(open=upload)) as opener, patch(
            "upark.storage.urlopen", return_value=io.BytesIO(self.payload)
        ) as download:
            code, link, error = self.cli("add", f"path={self.source}", "use=cloud")
        self.assertEqual(code, 0, error)
        self.assertRegex(link, r"/storage/v1/object/public/public/[a-f0-9]{32}/Ola-mundo.pdf\?download$")
        self.assertIsInstance(opener.call_args.args[0], NoRedirect)
        request = download.call_args.args[0]
        self.assertIsInstance(request, Request)
        self.assertEqual(request.full_url, link)
        self.assertIsNone(request.get_header("Authorization"))
        self.assertIsNone(request.get_header("Apikey"))

    def test_private_cloud_object_is_not_reported_as_success(self):
        self.create(["cloud", "1", "https://example.supabase.co", "private"], ["sb_secret_test"])
        with patch("upark.storage.build_opener", return_value=Mock(open=Mock(return_value=io.BytesIO()))), patch(
            "upark.storage.urlopen", side_effect=HTTPError("", 403, "Forbidden", {}, None)
        ):
            code, stdout, error = self.cli("add", f"path={self.source}", "use=cloud")
            self.assertEqual((code, stdout), (1, ""))
            self.assertIn("acesso público falhou (HTTP 403)", error)
            self.assertNotIn("sb_secret_test", error)

    def test_cloud_upload_does_not_follow_redirect(self):
        self.create(["cloud", "1", "https://example.supabase.co", "public"], ["sb_secret_test"])
        opener = Mock(open=Mock(side_effect=HTTPError("", 307, "Redirect", {}, None)))
        with patch("upark.storage.build_opener", return_value=opener):
            code, stdout, error = self.cli("add", f"path={self.source}", "use=cloud")
            self.assertEqual((code, stdout), (1, ""))
            self.assertIn("HTTP 307", error)
        self.assertIsNone(NoRedirect().redirect_request(None, None, 307, "", {}, "https://external.example"))

    def test_noninteractive_github_profile_uploads_and_checks_public_url(self):
        with patch("builtins.input", side_effect=AssertionError("cadastro deve ser sem prompts")), patch(
            "getpass.getpass", side_effect=AssertionError("segredo deve vir do ambiente")
        ):
            code, _, error = self.cli(
                "create", "--name", "automation", "--provider", "github",
                "--set", "repository=owner/project", "--set", "tag=demo",
                "--secret-env", "token=UPARK_TEST_TOKEN",
            )
        self.assertEqual(code, 0, error)
        with patch.dict(os.environ, {"UPARK_TEST_TOKEN": ""}):
            code, stdout, error = self.cli("add", f"path={self.source}", "use=automation")
            self.assertEqual((code, stdout), (1, ""))
            self.assertIn("UPARK_TEST_TOKEN", error)

        url = "https://github.com/owner/project/releases/download/demo/arquivo.pdf"
        requests = []

        def github_api(request, timeout):
            requests.append(request)
            self.assertEqual(request.get_header("Authorization"), "Bearer token-de-teste")
            if request.get_method() == "POST":
                self.assertEqual(request.data.read(), self.payload)
                self.assertEqual(request.get_header("Content-length"), str(len(self.payload)))
                response = {"browser_download_url": url}
            elif request.full_url.endswith("/releases/tags/demo"):
                response = {"id": 42, "draft": False}
            else:
                response = {"private": False}
            return io.BytesIO(json.dumps(response).encode())

        with patch.dict(os.environ, {"UPARK_TEST_TOKEN": "token-de-teste"}), patch(
            "upark.storage.build_opener", return_value=Mock(open=github_api)
        ), patch("upark.storage.urlopen", return_value=io.BytesIO(b"")) as download:
            self.assertEqual(self.cli("add", f"path={self.source}", "use=automation"), (0, url, ""))
        self.assertEqual(len(requests), 3)
        self.assertRegex(requests[-1].full_url, r"^https://uploads.github.com/repos/owner/project/releases/42/assets\?name=[a-f0-9]{32}-Ola-mundo.pdf$")
        self.assertEqual(download.call_args.args[0].full_url, url)
        self.assertIsNone(download.call_args.args[0].get_header("Authorization"))

    @unittest.skipUnless(find_spec("boto3"), "instale o extra s3 para testar o SDK")
    def test_s3_and_r2_upload(self):
        with http_server(StorageFixture) as (server, endpoint):
            for provider, name in (("2", "r2-profile"), ("3", "s3-profile")):
                with self.subTest(provider=provider):
                    region = [] if provider == "2" else ["us-east-1"]
                    keys = ["test-id", "test-secret"] if provider == "2" else ["test-id", "test-secret", ""]
                    self.create([name, provider, endpoint, *region, "bucket", endpoint], keys)
                    code, link, error = self.cli("add", f"path={self.source}", f"use={name}")
                    self.assertEqual(code, 0, error)
                    self.assertRegex(link, r"/[a-f0-9]{32}/Ola-mundo.pdf$")
                    self.assertTrue(server.upload_headers["Authorization"].startswith("AWS4-HMAC-SHA256"))
                    self.assertTrue(server.upload_path.startswith("/bucket/"))
                    self.assertNotIn("Authorization", server.download_headers)
                    with urlopen(link, timeout=5) as response:
                        self.assertEqual(response.read(), self.payload)
