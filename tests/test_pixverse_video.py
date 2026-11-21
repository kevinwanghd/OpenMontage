"""Contract tests for the PixVerse provider.

These lock in the parts that were wrong the first time the provider was used
ad hoc: the auth header is `API-KEY` (not Bearer), the .cn endpoint is the
default, and 480p is not a legal quality value. A regression here would only
surface as a 400 from the live API after money was already committed.
"""

from __future__ import annotations

import io
import json
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools.video import pixverse_video as pv  # noqa: E402


class FakeResponse:
    def __init__(self, body: bytes):
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self) -> bytes:
        return self._body


class ClientHeaderTests(unittest.TestCase):
    def test_auth_uses_api_key_header_not_bearer(self):
        client = pv.PixVerseClient(base_url="https://example.test/openapi/v2", api_key="k")
        headers = client._headers()
        self.assertEqual(headers["API-KEY"], "k")
        self.assertNotIn("Authorization", headers)

    def test_trace_id_is_unique_per_call(self):
        client = pv.PixVerseClient(base_url="https://example.test/openapi/v2", api_key="k")
        self.assertNotEqual(client._headers()["Ai-trace-id"], client._headers()["Ai-trace-id"])

    def test_base_url_defaults_to_cn_endpoint(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("PIXVERSE_BASE_URL", None)
            self.assertEqual(pv.PixVerseClient(api_key="k").base_url, pv.DEFAULT_BASE_URL)
        self.assertIn("pixverseai.cn", pv.DEFAULT_BASE_URL)

    def test_base_url_env_override_is_honoured(self):
        with mock.patch.dict(os.environ, {"PIXVERSE_BASE_URL": "https://alt.test/openapi/v2"}):
            self.assertEqual(pv.PixVerseClient(api_key="k").base_url, "https://alt.test/openapi/v2")

    def test_bare_host_gets_the_api_prefix(self):
        """The dashboard hands out a bare host; every endpoint lives under /openapi/v2."""
        with mock.patch.dict(os.environ, {"PIXVERSE_BASE_URL": "https://app-api.pixverseai.cn/"}):
            self.assertEqual(
                pv.PixVerseClient(api_key="k").base_url,
                "https://app-api.pixverseai.cn/openapi/v2",
            )


class SchemaTests(unittest.TestCase):
    def test_480p_is_not_offered(self):
        """The live API rejects 480p; offering it would fail only after a paid submit."""
        self.assertNotIn("480p", pv.QUALITIES)
        self.assertIn("540p", pv.QUALITIES)
        enum = pv.PixVerseVideo.input_schema["properties"]["quality"]["enum"]
        self.assertNotIn("480p", enum)

    def test_models_match_probed_values(self):
        self.assertEqual(set(pv.MODELS), {"v6", "v5.6", "v5", "v4.5", "v4", "c1"})


class DependencyTests(unittest.TestCase):
    def test_missing_key_names_the_variable(self):
        from tools.base_tool import DependencyError

        tool = pv.PixVerseVideo()
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("PIXVERSE_API_KEY", None)
            with self.assertRaises(DependencyError) as ctx:
                tool.check_dependencies()
        self.assertIn("PIXVERSE_API_KEY", str(ctx.exception))

    def test_missing_key_blocks_execute(self):
        tool = pv.PixVerseVideo()
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("PIXVERSE_API_KEY", None)
            result = tool.execute({"prompt": "x"})
        self.assertFalse(result.success)
        self.assertIn("PIXVERSE_API_KEY", result.error)


class PollTests(unittest.TestCase):
    def _client(self, payloads):
        client = pv.PixVerseClient(base_url="https://example.test/openapi/v2", api_key="k")
        responses = [FakeResponse(json.dumps(p).encode()) for p in payloads]

        def urlopen(request, timeout=None):
            return responses.pop(0)

        return client, urlopen

    def test_success_returns_resp(self):
        client, urlopen = self._client([{"ErrCode": 0, "Resp": {"status": 5}},
                                        {"ErrCode": 0, "Resp": {"status": 1, "url": "http://v/1.mp4"}}])
        with mock.patch.object(pv.urllib.request, "urlopen", urlopen), \
             mock.patch.object(pv.time, "sleep"):
            resp = client.wait_for_video(7, timeout_seconds=5, poll_interval=0)
        self.assertEqual(resp["url"], "http://v/1.mp4")

    def test_moderation_failure_is_not_retried(self):
        client, urlopen = self._client([{"ErrCode": 0, "Resp": {"status": 7}}])
        with mock.patch.object(pv.urllib.request, "urlopen", urlopen), \
             mock.patch.object(pv.time, "sleep"):
            with self.assertRaises(pv.PixVerseError) as ctx:
                client.wait_for_video(7, timeout_seconds=5, poll_interval=0)
        self.assertEqual(ctx.exception.code, "moderation")

    def test_unknown_status_raises_instead_of_hanging(self):
        client, urlopen = self._client([{"ErrCode": 0, "Resp": {"status": 42}}])
        with mock.patch.object(pv.urllib.request, "urlopen", urlopen), \
             mock.patch.object(pv.time, "sleep"):
            with self.assertRaises(pv.PixVerseError):
                client.wait_for_video(7, timeout_seconds=5, poll_interval=0)

    def test_timeout_raises_timeout_error(self):
        client, urlopen = self._client([{"ErrCode": 0, "Resp": {"status": 5}}] * 3)
        with mock.patch.object(pv.urllib.request, "urlopen", urlopen), \
             mock.patch.object(pv.time, "sleep"), \
             mock.patch.object(pv.time, "time", side_effect=[0, 0, 100]):
            with self.assertRaises(TimeoutError):
                client.wait_for_video(7, timeout_seconds=1, poll_interval=0)


class ErrorMappingTests(unittest.TestCase):
    def test_nonzero_errcode_raises_with_code(self):
        client = pv.PixVerseClient(base_url="https://example.test/openapi/v2", api_key="k")
        body = json.dumps({"ErrCode": 10004, "ErrMsg": "apiKey is empty"}).encode()
        with mock.patch.object(pv.urllib.request, "urlopen",
                               lambda request, timeout=None: FakeResponse(body)):
            with self.assertRaises(pv.PixVerseError) as ctx:
                client.text_to_video({"model": "v6"})
        self.assertEqual(ctx.exception.code, 10004)


class DiscoveryTests(unittest.TestCase):
    def test_tool_is_discovered_by_the_registry(self):
        from tools.tool_registry import ToolRegistry

        registry = ToolRegistry()
        registry.discover("tools")
        self.assertIn("pixverse_video", registry.list_all())


if __name__ == "__main__":
    unittest.main()