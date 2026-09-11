import unittest

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from llm_log.openrouter_observability import openrouter_metadata_middleware


class OpenRouterMetadataMiddlewareTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        async def handler(request):
            return web.json_response(
                {"metadata_header": request.headers.get("X-OpenRouter-Metadata")}
            )

        app = web.Application(middlewares=[openrouter_metadata_middleware])
        app.router.add_post("/{provider}/api/v1/chat/completions", handler)
        self.client = TestClient(TestServer(app))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()

    async def test_openrouter_requests_opt_into_router_metadata(self):
        response = await self.client.post("/openrouter/api/v1/chat/completions")
        payload = await response.json()
        self.assertEqual(payload["metadata_header"], "enabled")

    async def test_other_upstreams_do_not_receive_openrouter_header(self):
        response = await self.client.post("/openai/api/v1/chat/completions")
        payload = await response.json()
        self.assertIsNone(payload["metadata_header"])


if __name__ == "__main__":
    unittest.main()
