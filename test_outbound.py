import io
import threading
import time
import unittest
import urllib.error
from email.message import Message
from unittest.mock import patch

import outbound


class OutboundTests(unittest.TestCase):
    def setUp(self):
        outbound.MIN_INTERVAL = 1.0
        with outbound._condition:
            outbound._hosts.clear()

    def test_same_host_requests_are_serialized_and_spaced(self):
        outbound.MIN_INTERVAL = 0.03
        active = 0
        maximum = 0
        calls = []
        guard = threading.Lock()

        class Response(io.BytesIO):
            def close(self):
                super().close()
                time.sleep(0.015)

        def open_request(request, timeout):
            nonlocal active, maximum
            with guard:
                active += 1
                maximum = max(maximum, active)
                calls.append(time.monotonic())
            time.sleep(0.02)
            with guard:
                active -= 1
            return Response(b"ok")

        with patch.object(outbound.urllib.request, "urlopen", side_effect=open_request):
            threads = [threading.Thread(target=self._request) for _ in range(3)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

        self.assertEqual(maximum, 1)
        self.assertEqual(len(calls), 3)
        self.assertTrue(all(b - a >= outbound.MIN_INTERVAL - 0.005 for a, b in zip(calls, calls[1:])))

    def test_different_hosts_can_run_in_parallel(self):
        entered = []
        barrier = threading.Barrier(2)

        class Response(io.BytesIO):
            pass

        def open_request(request, timeout):
            entered.append(request.full_url if hasattr(request, "full_url") else request)
            barrier.wait(timeout=1)
            return Response(b"ok")

        with patch.object(outbound.urllib.request, "urlopen", side_effect=open_request):
            threads = [threading.Thread(target=self._request, args=(host,)) for host in ("a.test", "b.test")]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=2)
        self.assertEqual(len(entered), 2)
        self.assertTrue(all(not thread.is_alive() for thread in threads))

    def test_429_retry_after_sets_cooldown(self):
        headers = Message()
        headers["Retry-After"] = "30"
        error = urllib.error.HTTPError("https://limited.test/", 429, "Too Many Requests", headers, None)
        with patch.object(outbound.urllib.request, "urlopen", side_effect=error):
            with self.assertRaises(urllib.error.HTTPError):
                outbound.urlopen("https://limited.test/resource")
        with outbound._condition:
            remaining = outbound._hosts["limited.test"]["next"] - time.monotonic()
        self.assertGreaterEqual(remaining, 29)

    @staticmethod
    def _request(host="same.test"):
        with outbound.urlopen(f"https://{host}/") as response:
            response.read()


if __name__ == "__main__":
    unittest.main()
