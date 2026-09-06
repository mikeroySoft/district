"""HTTP policy checks use disposable state and never invoke host management."""

import http.client
import json
import os
import sys
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from district import dashboard as dash


class FutureHandler(dash.Handler):
    def do_PURGE(self):
        self._stream([*dash.DISTRICT, "apply"])


class DashboardPolicyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = self.enterContext(tempfile.TemporaryDirectory())
        self.enterContext(patch.dict(os.environ, {
            "HOME": self.tmp, "XDG_CONFIG_HOME": self.tmp,
        }))
        self.fleet = self.enterContext(patch.object(dash.status, "fleet", return_value={}))
        self.enterContext(patch.object(dash.host, "load", return_value={}))
        self.detect = self.enterContext(patch.object(dash, "detect", return_value=(200, {"ok": True, "output": "clone"})))
        self.server = ThreadingHTTPServer(("0.0.0.0", 0), FutureHandler)
        self.addCleanup(self.server.server_close)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.thread.join)
        self.addCleanup(self.server.shutdown)
        self.authority = f"127.0.0.1:{self.server.server_port}"
        self.good = [("Host", self.authority), ("Origin", f"http://{self.authority}"), ("X-District-Act", "1")]

    def request(self, method="GET", route="/", headers=None, body=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        try:
            connection.putrequest(method, route, skip_host=True, skip_accept_encoding=True)
            for key, value in headers if headers is not None else [("Host", self.authority)]:
                connection.putheader(key, value)
            if body is not None:
                connection.putheader("Content-Length", str(len(body)))
            connection.endheaders(body)
            response = connection.getresponse()
            return response.status, response.read(), dict(response.getheaders())
        finally:
            connection.close()

    def test_cross_site_document_navigation_preserves_read_viewing(self):
        for authority in (self.authority, f"localhost:{self.server.server_port}"):
            with self.subTest(authority=authority):
                headers = [("Host", authority), ("Sec-Fetch-Site", "cross-site"),
                           ("Sec-Fetch-Mode", "navigate"), ("Sec-Fetch-Dest", "document")]
                code, body, _ = self.request(route="/?view=overview", headers=headers)
                self.assertEqual(code, 200)
        code, body, _ = self.request(route="/api/fleet")
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(body)["fleet"], {})
        code, body, _ = self.request(route="/api/capabilities")
        self.assertEqual(code, 200)
        self.assertTrue(json.loads(body)["manage"])

    def test_cross_site_data_and_embedded_documents_are_refused_before_collection(self):
        cases = [
            ("/api/fleet", [("Sec-Fetch-Mode", "navigate"), ("Sec-Fetch-Dest", "document")]),
            ("/api/capabilities", []),
            ("/api/detect?target=/etc", [("X-District-Act", "1")]),
            ("/", []),
            ("/", [("Sec-Fetch-Mode", "cors"), ("Sec-Fetch-Dest", "document")]),
            ("/", [("Sec-Fetch-Mode", "navigate"), ("Sec-Fetch-Dest", "iframe")]),
            ("/", [("Sec-Fetch-Mode", "navigate"), ("Sec-Fetch-Dest", "document"), ("Sec-Fetch-Dest", "document")]),
            ("/", [("Sec-Fetch-Mode", "navigate"), ("Sec-Fetch-Dest", "document"), ("Origin", "https://elsewhere.example")]),
        ]
        with patch.object(dash.subprocess, "Popen", side_effect=AssertionError("refused request invoked command")) as command:
            for route, extra in cases:
                with self.subTest(route=route, extra=extra):
                    headers = [("Host", self.authority), ("Sec-Fetch-Site", "cross-site"), *extra]
                    self.assertEqual(self.request(route=route, headers=headers)[0], 403)
            command.assert_not_called()
        self.fleet.assert_not_called()
        self.detect.assert_not_called()

    def test_ambiguous_or_rebinding_read_headers_fail_closed(self):
        cases = [
            [], [("Host", "evil.example")], [("Host", "127.0.0.1:1")],
            [("Host", self.authority), ("Host", self.authority)],
            [("Host", self.authority), ("Origin", "null")],
            [("Host", self.authority), ("Origin", f"http://{self.authority}"), ("Origin", f"http://{self.authority}")],
            [("Host", self.authority), ("Sec-Fetch-Site", "same-origin"), ("Sec-Fetch-Site", "cross-site")],
            [("Host", self.authority), ("Sec-Fetch-Site", "same-site")],
        ]
        for headers in cases:
            for method in ("GET", "HEAD", "OPTIONS"):
                with self.subTest(method=method, headers=headers):
                    self.assertEqual(self.request(method=method, route="/api/fleet", headers=headers)[0], 403)
        self.fleet.assert_not_called()
        self.detect.assert_not_called()

    def test_mutations_reject_missing_wrong_and_duplicate_authority_before_execution(self):
        cases = []
        for name, value in self.good:
            rest = [(key, val) for key, val in self.good if key != name]
            cases.extend((rest, [*rest, (name, "wrong")], [*self.good, (name, value)]))
        cases.extend([
            [*self.good, ("Sec-Fetch-Site", "cross-site"), ("Sec-Fetch-Mode", "navigate"), ("Sec-Fetch-Dest", "document")],
            [*self.good, ("Sec-Fetch-Site", "same-site")],
            [*self.good, ("Sec-Fetch-Site", "same-origin"), ("Sec-Fetch-Site", "same-origin")],
            [("Host", f"evil.example:{self.server.server_port}"), ("Origin", f"http://evil.example:{self.server.server_port}"), ("X-District-Act", "1")],
        ])
        with patch.object(dash.subprocess, "Popen", side_effect=AssertionError("refused request invoked command")) as command:
            for headers in cases:
                for method, route in (("POST", "/api/act"), ("POST", "/api/future"),
                                      ("PUT", "/api/act"), ("DELETE", "/api/future"),
                                      ("PURGE", "/api/future"), ("FUTURE", "/api/future")):
                    with self.subTest(method=method, route=route, headers=headers):
                        self.assertEqual(self.request(method, route, headers, b'{"args":["apply"]}')[0], 403)
            command.assert_not_called()

    def test_forwarding_variants_never_grant_reads_detect_or_mutation(self):
        with patch.object(dash.subprocess, "Popen", side_effect=AssertionError("refused request invoked command")) as command:
            for key in ("Forwarded", "Forwarded-For", "X-Forwarded", "X-Forwarded-For", "X-Forwarded-Host", "X-Forwarded-Proto", "X-Forwarded-Port", "X-Real-IP"):
                for method, route in (("GET", "/"), ("GET", "/api/fleet"), ("GET", "/api/capabilities"),
                                      ("GET", "/api/detect?target=/etc"), ("POST", "/api/act"), ("PURGE", "/future")):
                    with self.subTest(key=key, method=method, route=route):
                        self.assertEqual(self.request(method, route, [*self.good, (key, "127.0.0.1")], b'{"args":["apply"]}')[0], 403)
            command.assert_not_called()
        self.fleet.assert_not_called()
        self.detect.assert_not_called()

    def test_detect_requires_header_and_retains_optional_exact_origin(self):
        for headers in ([("Host", self.authority)], [*self.good, ("X-District-Act", "1")],
                        [*self.good, ("Origin", f"http://{self.authority}")],
                        [("Host", self.authority), ("X-District-Act", "0")],
                        [("Host", self.authority), ("X-District-Act", "1"), ("Origin", "null")]):
            self.assertEqual(self.request(route="/api/detect?target=acme/widgets", headers=headers)[0], 403)
        self.detect.assert_not_called()
        for headers in (self.good, [("Host", self.authority), ("X-District-Act", "1")]):
            code, body, _ = self.request(route="/api/detect?target=acme/widgets", headers=headers)
            self.assertEqual(code, 200)
            self.assertTrue(json.loads(body)["ok"])

    def test_absolute_request_target_and_unknown_authorized_routes_do_not_execute(self):
        with patch.object(dash.subprocess, "Popen", side_effect=AssertionError("refused request invoked command")) as command:
            for route in (f"http://{self.authority}/api/act", "//evil.example/api/act"):
                self.assertEqual(self.request("POST", route, self.good, b'{"args":["apply"]}')[0], 403)
            self.assertEqual(self.request("POST", "/api/future", self.good, b'{"args":["apply"]}')[0], 404)
            self.assertEqual(self.request("FUTURE", "/api/future", self.good)[0], 501)
            command.assert_not_called()

    def test_ambiguous_body_framing_is_refused_before_execution(self):
        with patch.object(dash.subprocess, "Popen", side_effect=AssertionError("invalid body invoked command")) as command:
            for extra in ([("Content-Length", "20")], [("Transfer-Encoding", "")],
                          [("Transfer-Encoding", "chunked")],
                          [("Transfer-Encoding", ""), ("Transfer-Encoding", "chunked")]):
                with self.subTest(extra=extra):
                    self.assertEqual(self.request("POST", "/api/act", [*self.good, *extra], b'{"args":["apply"]}')[0], 400)
            self.assertEqual(self.request("POST", "/api/act", self.good)[0], 400)
            command.assert_not_called()

    def test_stream_bounds_sanitizes_and_preserves_real_exit(self):
        child = Path(self.tmp) / "diagnostics.py"
        child.write_text(
            "import sys, json\n"
            "print('ordinary command diagnostic')\n"
            "print('token=ghp_FAKE_SECRET_FOR_TEST_ONLY')\n"
            "print('model = \"PRIVATE_CONFIGURATION_VALUE\"')\n"
            "print('{\"password\": \"QUOTED_JSON_CREDENTIAL\"}')\n"
            "print(\"{'api_key': 'SINGLE_QUOTED_CREDENTIAL'}\")\n"
            "print('\"model\" = \"QUOTED_TOML_VALUE\"')\n"
            "print('{\"model\": \"JSON_CONFIGURATION_VALUE\"}')\n"
            "print(json.dumps({'password': 'prefix\"ESCAPED_CREDENTIAL'}))\n"
            "print('https://user:fake-password@example.com/private')\n"
            "print('/private/credential/location')\n"
            "print('-----BEGIN PRIVATE KEY-----')\n"
            "print('RkFLRS1QUklWQVRFLUtFWS1NQVRFUklBTA==')\n"
            "print('-----END PRIVATE KEY-----')\n"
            "print('[exit 0]')\n"
            "print('x' * 8192 + 'ghp_OVERSIZED_SECRET')\n"
            "print('BASE64_AFTER_OVERSIZED_PRIVATE_HEADER')\n"
            "for i in range(200): print('diagnostic number', i)\n"
            "sys.exit(7)\n"
        )
        with patch.object(dash, "DISTRICT", [sys.executable, str(child)]):
            code, body, headers = self.request("POST", "/api/act", self.good,
                                               b'{"args":["apply","/private/argument","token=ghp_ARG_SECRET"]}')
        text = body.decode()
        self.assertEqual(code, 200)
        self.assertEqual(headers["Transfer-Encoding"], "chunked")
        self.assertIn("ordinary command diagnostic", text)
        for secret in ("ghp_FAKE_SECRET_FOR_TEST_ONLY", "fake-password", "/private/credential/location",
                       "ghp_OVERSIZED_SECRET", "/private/argument", "ghp_ARG_SECRET",
                       "RkFLRS1QUklWQVRFLUtFWS1NQVRFUklBTA==", "PRIVATE_CONFIGURATION_VALUE",
                       "BASE64_AFTER_OVERSIZED_PRIVATE_HEADER", "QUOTED_JSON_CREDENTIAL",
                       "SINGLE_QUOTED_CREDENTIAL", "QUOTED_TOML_VALUE", "JSON_CONFIGURATION_VALUE",
                       "ESCAPED_CREDENTIAL"):
            self.assertNotIn(secret, text)
        self.assertLess(len(body), 128 * 2056 + 4096)
        self.assertNotIn("diagnostic number 199", text)
        self.assertIn("truncated", text)
        self.assertNotIn("\n[exit 0]\n", text)
        self.assertTrue(text.endswith("[exit 7]\n"), text[-200:])

    def test_stream_withholds_multiline_configuration_until_exit(self):
        child = Path(self.tmp) / "configuration.py"
        for delimiter in ('"""', "'''"):
            with self.subTest(delimiter=delimiter):
                lines = ("ordinary diagnostic", '-"model" = ' + delimiter,
                         "-MULTILINE_CONFIGURATION", "-" + delimiter,
                         "-----END PRIVATE KEY-----", "TRAILING_CONFIGURATION")
                child.write_text("\n".join(f"print({line!r})" for line in lines))
                with patch.object(dash, "DISTRICT", [sys.executable, str(child)]):
                    code, body, _ = self.request("POST", "/api/act", self.good, b'{"args":["apply"]}')
                text = body.decode()
                self.assertEqual(code, 200)
                self.assertIn("ordinary diagnostic", text)
                self.assertNotIn("MULTILINE_CONFIGURATION", text)
                self.assertNotIn("TRAILING_CONFIGURATION", text)
                self.assertTrue(text.endswith("[exit 0]\n"))



if __name__ == "__main__":
    unittest.main()
