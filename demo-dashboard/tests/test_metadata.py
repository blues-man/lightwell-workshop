"""Focused checks for the dashboard's authenticated Maven metadata lookup."""

import importlib.util
from pathlib import Path
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch
import urllib.request


spec = importlib.util.spec_from_file_location(
    "dashboard", Path(__file__).resolve().parents[1] / "server.py"
)
dashboard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(dashboard)


class MetadataTest(TestCase):
    def test_lightwell_metadata_uses_basic_auth_without_fetching_a_component(self):
        cfg = SimpleNamespace(
            lightwell="https://packages.example.test/lightwell/java/remediated",
            lightwell_user="account|user", lightwell_password="token",
            nexus="https://nexus.example.test", repo="remediated",
        )
        body = b"<metadata><versioning><versions><version>1.0.rhlw-00001</version></versions></versioning></metadata>"
        with patch.object(dashboard, "http", return_value=(200, body)) as request:
            versions = dashboard.remediated_versions(cfg, "example.group", "artifact")
        self.assertEqual(versions, ["1.0.rhlw-00001"])
        self.assertEqual(
            request.call_args.args[0],
            "https://packages.example.test/lightwell/java/remediated/example/group/artifact/maven-metadata.xml",
        )
        self.assertEqual(
            request.call_args.kwargs["headers"]["Authorization"],
            dashboard.basic("account|user", "token"),
        )

    def test_redirect_drops_credentials_when_origin_changes(self):
        req = urllib.request.Request(
            "https://packages.example.test/metadata.xml",
            headers={"Authorization": "Basic secret"},
        )
        handler = dashboard.SafeRedirectHandler()
        redirected = handler.redirect_request(
            req, None, 302, "Found", {}, "https://objects.example.test/signed.xml"
        )
        self.assertNotIn("Authorization", redirected.headers)

    def test_redirect_keeps_credentials_on_same_origin(self):
        req = urllib.request.Request(
            "https://packages.example.test/metadata.xml",
            headers={"Authorization": "Basic secret"},
        )
        handler = dashboard.SafeRedirectHandler()
        redirected = handler.redirect_request(
            req, None, 302, "Found", {}, "https://packages.example.test/next.xml"
        )
        self.assertEqual(redirected.headers["Authorization"], "Basic secret")
