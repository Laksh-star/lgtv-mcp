"""One-time pairing: find the TV, trigger the on-screen prompt, store the client key."""

from __future__ import annotations

import asyncio
import sys

from .tv import CONFIG_FILE, PAIRING_TIMEOUT, TV, TVError, arp_mac, discover, save_config


async def _pair(host: str | None) -> int:
    tv = TV()
    if host:
        tv.cfg["host"] = host
    if not tv.host:
        print("Searching for LG TVs (SSDP)...", flush=True)
        tvs = await discover()
        if not tvs:
            print("No LG TV found. Pass the IP: uv run lgtv-pair 192.168.1.x")
            return 1
        tv.cfg.update(tvs[0])
        print(f"Found {tvs[0].get('name') or 'LG TV'} at {tvs[0]['host']} (MAC {tvs[0].get('mac') or '?'})")
    if not tv.mac and (mac := await asyncio.to_thread(arp_mac, tv.host)):
        tv.cfg["mac"] = mac
    tv.cfg.pop("client_key", None) if "--force" in sys.argv else None
    save_config(tv.cfg)
    if not tv.cfg.get("client_key"):
        print(f">>> Accept the pairing prompt on the TV with your remote (you have {PAIRING_TIMEOUT} s).", flush=True)
    try:
        client = await tv.connect(allow_pairing=True)
    except TVError as e:
        print(f"Pairing failed: {e}")
        return 1
    await client.send_message("Claude is now paired with this TV")
    print(f"Paired. Client key saved to {CONFIG_FILE}")
    await tv.close()
    return 0


def main() -> None:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    sys.exit(asyncio.run(_pair(args[0] if args else None)))


if __name__ == "__main__":
    main()
