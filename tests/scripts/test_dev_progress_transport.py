"""Discord transport: reused connection, per-run GET cache, retry and ambiguity rules."""
import http.client
import importlib.util
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location(
    "dev_progress_board", Path(__file__).resolve().parents[2] / "scripts/dev_progress_board.py")
b = importlib.util.module_from_spec(SPEC)
sys.path.insert(0, str(Path(SPEC.origin).parent))
SPEC.loader.exec_module(b)
sys.path.pop(0)


class FakeResponse:
    def __init__(self, status, raw=b"{}", will_close=False):
        self.status, self.raw, self.will_close = status, raw, will_close

    def read(self, limit=None):
        return self.raw


class FakeConnection:
    def __init__(self, script):
        self.script, self.calls, self.closed = script, [], False

    def request(self, method, path, body=None, headers=None):
        self.calls.append((method, path))
        self.pending = self.script.pop(0)
        if isinstance(self.pending, Exception):
            raise self.pending

    def getresponse(self):
        return self.pending

    def close(self):
        self.closed = True


def api(script):
    d = b.Discord.__new__(b.Discord)
    d.guild_id, d.channel_id, d.token, d.threads, d.operations = "1", "2", "t", set(), b.collections.Counter()
    d.thread_states, d.read_cache = {}, {}
    d.fake = FakeConnection(script)
    d.connection = d.fake
    return d


class TransportTests(unittest.TestCase):
    def setUp(self):
        patcher = patch.object(b.time, "sleep", lambda _: None)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_repeat_get_uses_cache_and_copies(self):
        d = api([FakeResponse(200, b'{"a": 1}')])
        d.request("GET", "/channels/2")["a"] = 9
        self.assertEqual(d.request("GET", "/channels/2"), {"a": 1})
        self.assertEqual(len(d.fake.calls), 1)
        self.assertEqual(d.operations["GET"], 1)

    def test_write_clears_cache(self):
        d = api([FakeResponse(200, b'{"a": 1}'), FakeResponse(200, b"{}"), FakeResponse(200, b'{"a": 2}')])
        d.request("GET", "/channels/2")
        d.request("PATCH", "/channels/2", {"x": 1})
        self.assertEqual(d.request("GET", "/channels/2"), {"a": 2})

    def test_404_is_none_and_not_cached(self):
        d = api([FakeResponse(404), FakeResponse(200, b'{"a": 1}')])
        self.assertIsNone(d.request("GET", "/channels/2"))
        self.assertEqual(d.request("GET", "/channels/2"), {"a": 1})

    def test_429_retries(self):
        d = api([FakeResponse(429, b'{"retry_after": 1}'), FakeResponse(200, b'{"a": 1}')])
        self.assertEqual(d.request("GET", "/channels/2"), {"a": 1})

    def test_403_raises(self):
        d = api([FakeResponse(403)])
        with self.assertRaises(b.DiscordError):
            d.request("GET", "/channels/2")

    def test_post_failure_stays_ambiguous(self):
        d = api([http.client.RemoteDisconnected("gone")])
        with self.assertRaisesRegex(RuntimeError, "写入结果未知"):
            d.request("POST", "/channels/2/messages", {"x": 1})
        self.assertTrue(d.fake.closed)
        self.assertIsNone(d.connection)

    def test_stale_connection_get_reconnects(self):
        d = api([http.client.RemoteDisconnected("gone")])
        replacement = FakeConnection([FakeResponse(200, b'{"a": 1}')])
        d.connect = lambda: replacement
        self.assertEqual(d.request("GET", "/channels/2"), {"a": 1})
        self.assertEqual(len(replacement.calls), 1)

    def test_retries_exhausted(self):
        d = api([FakeResponse(500) for _ in range(4)])
        with self.assertRaisesRegex(RuntimeError, "重试耗尽"):
            d.request("GET", "/channels/2")

    def test_server_closing_connection_reconnects_next(self):
        d = api([FakeResponse(200, b'{"a": 1}', will_close=True)])
        d.request("GET", "/channels/2")
        self.assertIsNone(d.connection)

    def test_unauthorized_target_still_refused_before_cache(self):
        d = api([])
        with self.assertRaisesRegex(RuntimeError, "拒绝访问"):
            d.request("GET", "/channels/99")


if __name__ == "__main__":
    unittest.main()
