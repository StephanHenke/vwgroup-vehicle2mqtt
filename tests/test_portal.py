"""Portal migration and explicit-origin configuration regressions."""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from vw_euda_mqtt.api_constants import BASE_URL, LEGACY_BASE_URL, IDENTITY_BASE
from vw_euda_mqtt.api_login import _validate_login_url
from vw_euda_mqtt.api_models import AuthError
from vw_euda_mqtt.api_portal import normalize_portal_base_url
from vw_euda_mqtt.config import ServiceConfig


class PortalOriginTests(unittest.TestCase):
    def test_current_legacy_and_identity_origins_are_explicitly_trusted(self):
        for base in (BASE_URL, LEGACY_BASE_URL, IDENTITY_BASE):
            with self.subTest(base=base):
                _validate_login_url(base + "/login?code=example")
                _validate_login_url(base.upper() + ":443/login")

    def test_unknown_host_requires_an_explicit_override(self):
        target = "https://next-portal.example/login"
        with self.assertRaises(AuthError):
            _validate_login_url(target)
        _validate_login_url(target, portal_base_url="https://next-portal.example")

    def test_untrusted_schemes_ports_userinfo_and_lookalikes_stay_blocked(self):
        for url in (
            "http://identity.vwgroup.io/login", "https://identity.vwgroup.io:444/login",
            "https://user:secret@identity.vwgroup.io/login", "https://identity.vwgroup.io.example/login",
            BASE_URL + ".example/login", "https://eu-data-act.vwgroup-datahub.com@evil.example/login",
            "https://untrusted.vwgroup-datahub.com/login", "https://identity.vwgroup.io\\@evil.example/login",
            "https://identity.vwgroup.io:not-a-port/login", "//identity.vwgroup.io/login", "file:///login",
        ):
            with self.subTest(url=url), self.assertRaises(AuthError):
                _validate_login_url(url)

    def test_error_never_echoes_credentials_or_authentication_parameters(self):
        url = "https://private-user:private-password@evil.example/private-path?code=private-code#private-fragment"
        with self.assertRaises(AuthError) as caught:
            _validate_login_url(url)
        message = str(caught.exception)
        self.assertIn("evil.example", message)
        for secret in ("private-user", "private-password", "private-path", "private-code", "private-fragment"):
            self.assertNotIn(secret, message)

    def test_base_url_normalizes_only_a_plain_https_origin(self):
        self.assertEqual(normalize_portal_base_url("https://NEXT-PORTAL.example:443/"), "https://next-portal.example")
        for value in ("", None, 1, "http://next-portal.example", "https://user:secret@next-portal.example",
                      "https://next-portal.example/path", "https://next-portal.example?code=secret",
                      "https://next-portal.example#fragment", "https://next-portal.example:444",
                      " https://next-portal.example", "https://next-portal.example\\path", "https://[broken"):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "HTTPS origin"):
                normalize_portal_base_url(value)


class PortalConfigTests(unittest.TestCase):
    def load(self, raw, env=None):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(json.dumps({"email": "user@example.invalid", "password": "unused", **raw}))
            with patch.dict(os.environ, env or {}, clear=True):
                return ServiceConfig.from_file(path)

    def test_existing_config_defaults_to_current_portal(self):
        config = self.load({})
        self.assertEqual(config.portal_base_url, BASE_URL)
        self.assertEqual(config.portal.base_url, BASE_URL)
        self.assertFalse(config.mqtt.retain)

    def test_environment_override_wins_over_config(self):
        config = self.load({"portal_base_url": "https://configured.example"},
                           {"VW_EUDA_PORTAL_BASE_URL": "https://NEXT-PORTAL.example:443/"})
        self.assertEqual(config.portal_base_url, "https://next-portal.example")
        self.assertEqual(config.portal.base_url, config.portal_base_url)

    def test_config_override_and_empty_environment(self):
        config = self.load({"portal_base_url": "https://configured.example/"}, {"VW_EUDA_PORTAL_BASE_URL": ""})
        self.assertEqual(config.portal.base_url, "https://configured.example")

    def test_invalid_override_fails_before_network_access_without_echoing_its_value(self):
        with self.assertRaises(ValueError) as caught:
            self.load({}, {"VW_EUDA_PORTAL_BASE_URL": "https://user:secret@next-portal.example"})
        self.assertNotIn("secret", str(caught.exception))
        self.assertNotIn("user:", str(caught.exception))

    def test_example_uses_the_current_portal(self):
        path = Path(__file__).resolve().parents[1] / "config.example.json"
        self.assertEqual(json.loads(path.read_text())["portal_base_url"], BASE_URL)
