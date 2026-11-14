import asyncio
import json

import daemon_client as client
import pytest


def test_client_transmits_mode_on_process_and_cannot_override_it(socket_path):
    async def scenario():
        requests = []

        async def serve(reader, writer):
            request = json.loads(await reader.readline())
            requests.append(request)
            writer.write((json.dumps({"id": request["id"], "ok": False, "error": "mode mismatch"}) + "\n").encode())
            await writer.drain()
            writer.close()

        server = await asyncio.start_unix_server(serve, str(socket_path))
        try:
            c = await client.DaemonClient.connect(cloud=True)
            try:
                with pytest.raises(client.DaemonError, match="mode mismatch"):
                    await c.request("process", path="synthetic.wav", cloud=False)
            finally:
                await c.close()
        finally:
            server.close()
            await server.wait_closed()
        assert requests[0]["cloud"] is True

    asyncio.run(scenario())
