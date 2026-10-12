#!/usr/bin/env python3
"""Relay the local Music Studio (127.0.0.1:7860) onto this machine's Tailscale IP, port 7860.
Listens only on the tailnet address, so it is reachable from tailnet devices and not from the LAN.
No root needed. Usage: tailnet-forward.py [tailscale-ip]"""
import asyncio, subprocess, sys

TARGET = ("127.0.0.1", 7860)


async def pipe(reader, writer):
    try:
        while data := await reader.read(65536):
            writer.write(data)
            await writer.drain()
    except (ConnectionError, asyncio.CancelledError):
        pass
    finally:
        writer.close()


async def handle(client_r, client_w):
    try:
        server_r, server_w = await asyncio.open_connection(*TARGET)
    except OSError:
        client_w.close()
        return
    await asyncio.gather(pipe(client_r, server_w), pipe(server_r, client_w))


async def main():
    ip = sys.argv[1] if len(sys.argv) > 1 else subprocess.check_output(["tailscale", "ip", "-4"], text=True).split()[0]
    server = await asyncio.start_server(handle, ip, TARGET[1])
    print(f"Radio on the tailnet: http://{ip}:{TARGET[1]}/radio", flush=True)
    async with server:
        await server.serve_forever()


asyncio.run(main())
