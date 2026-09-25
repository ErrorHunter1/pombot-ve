"""WebSocket-Konsolen: VNC-Proxy (KVM) und PTY-Terminal (LXC / Host-Shell)."""
import asyncio
import fcntl
import json
import os
import pty
import struct
import subprocess
import termios

from fastapi import WebSocket
from starlette.websockets import WebSocketDisconnect


async def _pump(tasks):
    done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    for t in pending:
        t.cancel()
    for t in done:
        exc = t.exception()
        if exc and not isinstance(exc, (WebSocketDisconnect, ConnectionError)):
            raise exc


async def vnc_proxy(ws: WebSocket, port: int) -> None:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)

    async def ws_to_tcp():
        while True:
            msg = await ws.receive()
            if msg["type"] == "websocket.disconnect":
                return
            data = msg.get("bytes") or (msg.get("text") or "").encode()
            writer.write(data)
            await writer.drain()

    async def tcp_to_ws():
        while True:
            data = await reader.read(65536)
            if not data:
                return
            await ws.send_bytes(data)

    try:
        await _pump([asyncio.create_task(ws_to_tcp()), asyncio.create_task(tcp_to_ws())])
    finally:
        writer.close()


async def pty_session(ws: WebSocket, argv: list[str]) -> None:
    """Startet `argv` in einem Pseudo-Terminal. Protokoll zum Browser:
    Server -> Client: Binärdaten (Terminalausgabe)
    Client -> Server: JSON {"type": "input", "data": "..."} oder {"type": "resize", "cols": 80, "rows": 24}
    """
    master, slave = pty.openpty()
    env = {"TERM": "xterm-256color", "LANG": "C.UTF-8",
           "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin", "HOME": "/root"}

    def _ctty():
        fcntl.ioctl(0, termios.TIOCSCTTY, 0)

    proc = subprocess.Popen(argv, stdin=slave, stdout=slave, stderr=slave, env=env,
                            start_new_session=True, preexec_fn=_ctty, close_fds=True)
    os.close(slave)
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[bytes] = asyncio.Queue()

    def on_readable():
        try:
            data = os.read(master, 65536)
        except OSError:
            data = b""
        queue.put_nowait(data)
        if not data:
            loop.remove_reader(master)

    loop.add_reader(master, on_readable)

    async def pty_to_ws():
        while True:
            data = await queue.get()
            if not data:
                return
            await ws.send_bytes(data)

    async def ws_to_pty():
        while True:
            msg = await ws.receive()
            if msg["type"] == "websocket.disconnect":
                return
            if msg.get("bytes"):
                os.write(master, msg["bytes"])
                continue
            try:
                payload = json.loads(msg.get("text") or "{}")
            except ValueError:
                continue
            if payload.get("type") == "input":
                os.write(master, str(payload.get("data", "")).encode())
            elif payload.get("type") == "resize":
                rows, cols = int(payload.get("rows", 24)), int(payload.get("cols", 80))
                fcntl.ioctl(master, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))

    try:
        await _pump([asyncio.create_task(pty_to_ws()), asyncio.create_task(ws_to_pty())])
    finally:
        loop.remove_reader(master)
        if proc.poll() is None:
            proc.terminate()
            try:
                await asyncio.wait_for(asyncio.to_thread(proc.wait), 3)
            except asyncio.TimeoutError:
                proc.kill()
        os.close(master)
