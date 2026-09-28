import asyncio
import base64
import itertools
import json
from collections.abc import AsyncIterator

import numpy as np

from wav2sum.config import MAX_MESSAGE, SOCKET_PATH


class DaemonError(RuntimeError):
    pass


class DaemonClient:
    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        self._reader, self._writer = reader, writer
        self._ids = itertools.count(1)
        self._pending: dict[int, asyncio.Future] = {}
        self._events: asyncio.Queue[dict | None] = asyncio.Queue()
        self._reading = asyncio.create_task(self._read())

    @classmethod
    async def connect(cls) -> "DaemonClient":
        try:
            reader, writer = await asyncio.open_unix_connection(str(SOCKET_PATH), limit=MAX_MESSAGE)
        except (FileNotFoundError, ConnectionRefusedError):
            raise DaemonError("фоновый процесс не запущен (wav2sum serve или иконка в меню-баре)") from None
        return cls(reader, writer)

    async def request(self, cmd: str, **params) -> dict:
        request_id = next(self._ids)
        future = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        self._writer.write((json.dumps({"id": request_id, "cmd": cmd, **params}, ensure_ascii=False) + "\n").encode())
        await self._writer.drain()
        reply = await future
        reply.pop("id", None)
        if not reply.pop("ok"):
            raise DaemonError(reply["error"])
        return reply

    async def dictate(self, audio: np.ndarray, **context) -> dict:
        pcm = (np.clip(audio, -1, 1) * 32767).astype("<i2").tobytes()
        return await self.request("dictate", audio=base64.b64encode(pcm).decode(), **context)

    async def events(self) -> AsyncIterator[dict]:
        await self.request("subscribe")
        while (event := await self._events.get()) is not None:
            yield event

    async def close(self) -> None:
        self._writer.close()
        self._reading.cancel()

    async def _read(self) -> None:
        try:
            while line := await self._reader.readline():
                message = json.loads(line)
                if "event" in message:
                    self._events.put_nowait(message)
                elif future := self._pending.pop(message.get("id"), None):
                    future.set_result(message)
        finally:
            self._events.put_nowait(None)
            for future in self._pending.values():
                future.set_exception(DaemonError("соединение с фоновым процессом потеряно"))
