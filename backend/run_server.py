"""Convenience entry point: python run_server.py"""

from __future__ import annotations

import socket

import uvicorn

from app.config import settings


def lan_address() -> str | None:
    """This machine's address on the practice network, for the phone to open.

    Uses a UDP socket to a public address purely to ask the routing table which
    interface would be used; nothing is actually sent.
    """
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("10.255.255.255", 1))
        return probe.getsockname()[0]
    except OSError:
        return None
    finally:
        probe.close()


if __name__ == "__main__":
    if settings.host in {"0.0.0.0", "::"}:
        address = lan_address()
        if address:
            print(f"\n  Chair-side view : http://{address}:{settings.port}/")
            print(f"  Phone capture   : http://{address}:{settings.port}/capture.html")
            print("  (open that second link on a phone on the same Wi-Fi)\n")

    uvicorn.run("app.main:app", host=settings.host, port=settings.port, reload=False)
