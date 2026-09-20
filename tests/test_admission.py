import asyncio
import tempfile
import unittest
from pathlib import Path

from aiohttp import ClientSession, web

from llm_log.admission import AdmissionPolicy, AdmissionRejected, AdmissionScheduler
from llm_log.proxy import build_app
from llm_log.recorder import RecorderActor


class AdmissionSchedulerTest(unittest.IsolatedAsyncioTestCase):
    async def test_fifo_queue_full_and_release(self):
        scheduler = AdmissionScheduler(
            AdmissionPolicy(
                max_active=1,
                max_queue_depth=1,
                queue_timeout_seconds=0.2,
                requests_per_minute=0,
                burst=1,
            )
        )

        first = await scheduler.acquire("alpha")
        second_task = asyncio.create_task(scheduler.acquire("alpha"))
        await asyncio.sleep(0)

        with self.assertRaises(AdmissionRejected) as raised:
            await scheduler.acquire("alpha")
        self.assertEqual(raised.exception.reason, "queue_full")
        self.assertGreaterEqual(raised.exception.retry_after, 1)

        await first.release()
        second = await asyncio.wait_for(second_task, timeout=0.2)
        await second.release()

        snapshot = await scheduler.snapshot("alpha")
        self.assertEqual(snapshot["active"], 0)
        self.assertEqual(snapshot["queued"], 0)

    async def test_queue_timeout_does_not_admit(self):
        scheduler = AdmissionScheduler(
            AdmissionPolicy(
                max_active=1,
                max_queue_depth=1,
                queue_timeout_seconds=0.03,
                requests_per_minute=0,
                burst=1,
            )
        )
        first = await scheduler.acquire("alpha")
        with self.assertRaises(AdmissionRejected) as raised:
            await scheduler.acquire("alpha")
        self.assertEqual(raised.exception.reason, "queue_timeout")
        await first.release()

    async def test_rate_token_is_not_refunded_on_release(self):
        scheduler = AdmissionScheduler(
            AdmissionPolicy(
                max_active=4,
                max_queue_depth=1,
                queue_timeout_seconds=0.02,
                requests_per_minute=60,
                burst=1,
            )
        )
        first = await scheduler.acquire("alpha")
        await first.release()

        with self.assertRaises(AdmissionRejected) as raised:
            await scheduler.acquire("alpha")
        self.assertEqual(raised.exception.reason, "queue_timeout")
        self.assertGreaterEqual(raised.exception.retry_after, 1)

    async def test_aliases_share_one_admission_group(self):
        scheduler = AdmissionScheduler(
            AdmissionPolicy(
                max_active=1,
                max_queue_depth=1,
                queue_timeout_seconds=0.2,
                requests_per_minute=0,
                burst=1,
                provider_groups={"opencode-zai": "zai", "zai-coding": "zai"},
            )
        )
        first = await scheduler.acquire("opencode-zai")
        second_task = asyncio.create_task(scheduler.acquire("zai-coding"))
        await asyncio.sleep(0)

        shared = await scheduler.snapshot("opencode-zai")
        self.assertEqual(shared["active"], 1)
        self.assertEqual(shared["queued"], 1)

        await first.release()
        second = await asyncio.wait_for(second_task, timeout=0.2)
        await second.release()

    async def test_cancelled_waiter_is_removed(self):
        scheduler = AdmissionScheduler(
            AdmissionPolicy(
                max_active=1,
                max_queue_depth=1,
                queue_timeout_seconds=1,
                requests_per_minute=0,
                burst=1,
            )
        )
        first = await scheduler.acquire("alpha")
        waiter = asyncio.create_task(scheduler.acquire("alpha"))
        await asyncio.sleep(0)
        waiter.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await waiter

        snapshot = await scheduler.snapshot("alpha")
        self.assertEqual(snapshot["queued"], 0)
        await first.release()


class ProxyAdmissionTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.release_first = asyncio.Event()
        self.upstream_hits = 0

        upstream = web.Application()
        upstream.router.add_post("/hold", self.hold)
        upstream.router.add_post("/stream", self.stream)
        self.upstream_runner = web.AppRunner(upstream)
        await self.upstream_runner.setup()
        self.upstream_site = web.TCPSite(self.upstream_runner, "127.0.0.1", 0)
        await self.upstream_site.start()
        port = self.upstream_site._server.sockets[0].getsockname()[1]
        self.upstream_url = f"http://127.0.0.1:{port}"

        recorder = RecorderActor(self.root)
        policy = AdmissionPolicy(
            max_active=1,
            max_queue_depth=1,
            queue_timeout_seconds=0.05,
            requests_per_minute=0,
            burst=1,
        )
        self.proxy = build_app(
            {"test": self.upstream_url},
            recorder,
            classifier=None,
            admission_policy=policy,
        )
        self.proxy_runner = web.AppRunner(self.proxy)
        await self.proxy_runner.setup()
        self.proxy_site = web.TCPSite(self.proxy_runner, "127.0.0.1", 0)
        await self.proxy_site.start()
        proxy_port = self.proxy_site._server.sockets[0].getsockname()[1]
        self.proxy_url = f"http://127.0.0.1:{proxy_port}"

    async def asyncTearDown(self):
        await self.proxy_runner.cleanup()
        await self.upstream_runner.cleanup()
        self.tmp.cleanup()

    async def hold(self, request):
        self.upstream_hits += 1
        await request.read()
        await self.release_first.wait()
        return web.json_response({"ok": True})

    async def stream(self, request):
        self.upstream_hits += 1
        await request.read()
        response = web.StreamResponse(
            status=200, headers={"Content-Type": "text/event-stream"}
        )
        await response.prepare(request)
        await response.write(b'data: {"delta":"queued-then-streamed"}\n\n')
        await response.write_eof()
        return response

    async def test_queue_full_and_timeout_return_local_429_without_upstream(self):
        async with ClientSession() as session:
            first = asyncio.create_task(
                session.post(f"{self.proxy_url}/test/hold", data=b"first")
            )
            for _ in range(100):
                if self.upstream_hits == 1:
                    break
                await asyncio.sleep(0.001)
            self.assertEqual(self.upstream_hits, 1)

            second = asyncio.create_task(
                session.post(f"{self.proxy_url}/test/stream", data=b"second")
            )
            await asyncio.sleep(0)

            async with session.post(
                f"{self.proxy_url}/test/stream", data=b"third"
            ) as third:
                self.assertEqual(third.status, 429)
                self.assertIn("Retry-After", third.headers)
                self.assertEqual(third.headers.get("Cache-Control"), "no-store")
                await third.read()

            second_response = await second
            self.assertEqual(second_response.status, 429)
            self.assertIn("Retry-After", second_response.headers)
            await second_response.read()
            self.assertEqual(self.upstream_hits, 1)

            self.release_first.set()
            first_response = await first
            self.assertEqual(first_response.status, 200)
            await first_response.read()

    async def test_queued_sse_is_not_committed_until_admitted(self):
        # Give the queued request enough time to be admitted after the active
        # request releases, proving ordinary SSE can wait without pre-committing
        # a queue-status response.
        self.proxy_runner and await self.proxy_runner.cleanup()

        recorder = RecorderActor(self.root / "sse")
        policy = AdmissionPolicy(
            max_active=1,
            max_queue_depth=1,
            queue_timeout_seconds=0.5,
            requests_per_minute=0,
            burst=1,
        )
        app = build_app(
            {"test": self.upstream_url},
            recorder,
            classifier=None,
            admission_policy=policy,
        )
        self.proxy_runner = web.AppRunner(app)
        await self.proxy_runner.setup()
        site = web.TCPSite(self.proxy_runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        self.proxy_url = f"http://127.0.0.1:{port}"
        self.release_first = asyncio.Event()
        self.upstream_hits = 0

        async with ClientSession() as session:
            first = asyncio.create_task(
                session.post(f"{self.proxy_url}/test/hold", data=b"first")
            )
            for _ in range(100):
                if self.upstream_hits == 1:
                    break
                await asyncio.sleep(0.001)

            queued = asyncio.create_task(
                session.post(f"{self.proxy_url}/test/stream", data=b"queued")
            )
            await asyncio.sleep(0.02)
            self.assertFalse(queued.done())
            self.assertEqual(self.upstream_hits, 1)

            self.release_first.set()
            first_response = await first
            await first_response.read()

            stream_response = await asyncio.wait_for(queued, timeout=0.5)
            self.assertEqual(stream_response.status, 200)
            body = await stream_response.read()
            self.assertIn(b"queued-then-streamed", body)
            self.assertEqual(self.upstream_hits, 2)


if __name__ == "__main__":
    unittest.main()
