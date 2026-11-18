"""Connection manager for an LG webOS TV: config, discovery, pairing, Wake-on-LAN."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import socket
import subprocess
import time
from pathlib import Path
from typing import Any
from urllib.parse import unquote

import aiohttp
import aiowebostv.webos_client as _wc
from aiowebostv import WebOsClient
from aiowebostv.exceptions import WebOsTvPairError
from wakeonlan import send_magic_packet

log = logging.getLogger("lgtv_mcp")

CONFIG_DIR = Path(os.environ.get("LGTV_CONFIG_DIR") or Path.home() / ".config" / "lgtv").expanduser()
CONFIG_FILE = CONFIG_DIR / "config.json"
SSAP_PORT = 3001
PAIRING_TIMEOUT = 60  # seconds the user has to accept the on-screen prompt


class TVError(Exception):
    """An error whose message is safe and useful to show to the user as-is."""


class TVUnavailable(TVError):
    pass


class TVNotPaired(TVError):
    pass


# ---------------------------------------------------------------- config


def load_config() -> dict[str, Any]:
    cfg: dict[str, Any] = {}
    if CONFIG_FILE.exists():
        try:
            cfg = json.loads(CONFIG_FILE.read_text())
        except (OSError, json.JSONDecodeError) as e:
            log.warning("could not read %s: %s", CONFIG_FILE, e)
    # Environment variables override the stored values.
    if os.environ.get("LGTV_HOST"):
        cfg["host"] = os.environ["LGTV_HOST"]
    if os.environ.get("LGTV_MAC"):
        cfg["mac"] = os.environ["LGTV_MAC"]
    return cfg


def save_config(cfg: dict[str, Any]) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    os.chmod(CONFIG_DIR, 0o700)
    tmp = CONFIG_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(cfg, indent=2) + "\n")
    os.chmod(tmp, 0o600)
    tmp.replace(CONFIG_FILE)


# ---------------------------------------------------------------- discovery


def _ssdp_search(timeout: float = 3.0) -> list[dict[str, str]]:
    """Find LG webOS TVs on the LAN via SSDP. Blocking; run in a thread."""
    st = "urn:lge-com:service:webos-second-screen:1"
    msg = (
        "M-SEARCH * HTTP/1.1\r\nHOST: 239.255.255.250:1900\r\n"
        f'MAN: "ssdp:discover"\r\nMX: 2\r\nST: {st}\r\n\r\n'
    ).encode()
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
    sock.settimeout(0.5)
    found: dict[str, dict[str, str]] = {}
    try:
        for _ in range(2):
            sock.sendto(msg, ("239.255.255.250", 1900))
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            try:
                data, (ip, _port) = sock.recvfrom(65507)
            except TimeoutError:
                continue
            text = data.decode(errors="ignore")
            if "webos" not in text.lower() and "lge" not in text.lower():
                continue
            name = ""
            m = re.search(r"^DLNADeviceName\.lge\.com:\s*(.+)$", text, re.I | re.M)
            if m:
                name = unquote(m.group(1).strip())
            found.setdefault(ip, {"host": ip, "name": name})
    finally:
        sock.close()
    for tv in found.values():
        tv["mac"] = arp_mac(tv["host"]) or ""
    return list(found.values())


def arp_mac(ip: str) -> str | None:
    """Look up a host's MAC address in the ARP cache (pinging it first)."""
    subprocess.run(["ping", "-c", "1", "-t", "1", ip], capture_output=True, check=False)
    out = subprocess.run(["arp", "-n", ip], capture_output=True, text=True, check=False).stdout
    m = re.search(r"at ([0-9a-f:]{11,17})", out, re.I)
    if not m or "incomplete" in out:
        return None
    # macOS drops leading zeros (3c:f0:3:...); normalise to two digits per octet.
    return ":".join(p.zfill(2) for p in m.group(1).lower().split(":"))


async def discover(timeout: float = 3.0) -> list[dict[str, str]]:
    return await asyncio.to_thread(_ssdp_search, timeout)


async def port_open(host: str, port: int = SSAP_PORT, timeout: float = 1.5) -> bool:
    try:
        _r, w = await asyncio.wait_for(asyncio.open_connection(host, port), timeout)
    except (OSError, TimeoutError):
        return False
    w.close()
    try:
        await w.wait_closed()
    except OSError:
        pass
    return True


# ---------------------------------------------------------------- manager


class TV:
    """Holds one persistent connection to the TV and reconnects on demand."""

    def __init__(self) -> None:
        self.cfg = load_config()
        self.client: WebOsClient | None = None
        self._lock = asyncio.Lock()

    @property
    def host(self) -> str | None:
        return self.cfg.get("host")

    @property
    def mac(self) -> str | None:
        return self.cfg.get("mac")

    def unreachable_message(self) -> str:
        where = f" at {self.host}" if self.host else ""
        return (
            f"The TV{where} is off or unreachable on the network. "
            "If it is off, call power_on to wake it (Wake-on-LAN)."
        )

    async def _rediscover(self) -> bool:
        """The TV may have a new DHCP address: look for it again via SSDP."""
        tvs = await discover()
        if not tvs:
            return False
        pick = None
        if self.mac:
            pick = next((t for t in tvs if t.get("mac") == self.mac), None)
        if pick is None and len(tvs) == 1 and not self.mac:
            pick = tvs[0]
        if pick is None or pick["host"] == self.host:
            return False
        log.info("TV moved from %s to %s", self.host, pick["host"])
        self.cfg["host"] = pick["host"]
        if pick.get("mac"):
            self.cfg["mac"] = pick["mac"]
        save_config(self.cfg)
        return True

    async def _drop_client(self) -> None:
        if self.client is None:
            return
        client, self.client = self.client, None
        try:
            await client.disconnect()
            await client.close_client_session()
        except Exception:  # noqa: BLE001 - best-effort cleanup
            pass

    async def connect(self, *, allow_pairing: bool = False) -> WebOsClient:
        async with self._lock:
            if self.client is not None and self.client.is_connected():
                return self.client
            await self._drop_client()

            if not self.host or not await port_open(self.host):
                if not await self._rediscover() or not await port_open(self.host):
                    if not self.host:
                        raise TVUnavailable(
                            "No LG TV found on the network. Make sure it is on and on the "
                            "same Wi-Fi, or set LGTV_HOST."
                        )
                    raise TVUnavailable(self.unreachable_message())

            if not self.cfg.get("client_key") and not allow_pairing:
                raise TVNotPaired(
                    "This Mac is not paired with the TV yet. Run `uv run lgtv-pair` in the "
                    "lgtv-mcp project folder and accept the prompt on the TV."
                )

            if allow_pairing:
                _wc.RECEIVE_TIMEOUT = PAIRING_TIMEOUT
            client = WebOsClient(self.host, self.cfg.get("client_key"), connect_timeout=5)
            try:
                await client.connect()
            except WebOsTvPairError as e:
                await client.close_client_session()
                raise TVNotPaired(
                    f"Pairing was rejected or not accepted in time ({e}). "
                    "Try again and press OK on the TV prompt."
                ) from e
            except (TimeoutError, OSError, aiohttp.ClientError) as e:
                await client.close_client_session()
                raise TVUnavailable(self.unreachable_message()) from e

            if client.client_key and client.client_key != self.cfg.get("client_key"):
                self.cfg["client_key"] = client.client_key
                save_config(self.cfg)
            if not self.cfg.get("mac") and (mac := await asyncio.to_thread(arp_mac, self.host)):
                self.cfg["mac"] = mac
                save_config(self.cfg)
            self.client = client
            return client

    async def wake(self) -> None:
        if not self.mac:
            raise TVError(
                "Don't know the TV's MAC address, so Wake-on-LAN can't be sent. "
                "Set LGTV_MAC or add \"mac\" to ~/.config/lgtv/config.json."
            )
        targets = {"255.255.255.255"}
        if self.host:
            targets.add(".".join(self.host.split(".")[:3] + ["255"]))
        for _ in range(3):
            for ip in targets:
                await asyncio.to_thread(send_magic_packet, self.mac, ip_address=ip)
            await asyncio.sleep(0.3)

    async def ensure_on(self, timeout: float = 30) -> str:
        """Turn the TV on (Wake-on-LAN, plus SSAP turnOn from standby) and wait until it is on.

        Keeps resending Wake-on-LAN while the TV is off the network, and turnOn every few seconds
        while it sits in standby (one request is sometimes ignored). The error says which stage failed.
        """
        if self.client is not None and self.client.is_connected() and self.client.tv_state.is_on:
            return "The TV is already on."
        await self.wake()
        loop = asyncio.get_running_loop()
        start = loop.time()
        deadline = start + timeout
        wol_sent, turn_on_sent = 1, 0
        reachable_at: float | None = None  # when port 3001 first answered
        standby_state: str | None = None   # last standby state the TV reported
        last_turn_on = -1e9
        connect_error: str | None = None
        while loop.time() < deadline:
            if self.host and await port_open(self.host, timeout=1):
                if reachable_at is None:
                    reachable_at = loop.time() - start
                try:
                    client = await self.connect()
                except TVError as e:
                    connect_error = str(e)
                    await asyncio.sleep(1.5)
                    continue
                s = client.tv_state
                if s.is_on:
                    return (f"The TV is on ({s.power_state.get('state', 'On')}) after "
                            f"{loop.time() - start:.0f} s.")
                standby_state = (s.power_state or {}).get("state") or "standby"
                if loop.time() - last_turn_on >= 5:  # reachable but in standby: ask it to turn on
                    last_turn_on = loop.time()
                    turn_on_sent += 1
                    try:
                        await client.power_on()
                    except Exception:  # noqa: BLE001 - not all states accept it
                        pass
            else:
                await self.wake()
                wol_sent += 1
            await asyncio.sleep(1.5)

        if reachable_at is None:
            why = (f"it never answered on the network ({wol_sent} Wake-on-LAN bursts sent). It was probably "
                   "fully powered down or unplugged. Check that 'TV On With Mobile' (Settings > General > "
                   "Devices > External Devices) is on and the TV has power")
        elif standby_state:
            why = (f"it was on the network after {reachable_at:.0f} s but stayed in '{standby_state}' "
                   f"despite {turn_on_sent} turn-on request{'s' if turn_on_sent != 1 else ''}")
        else:
            why = (f"it was on the network after {reachable_at:.0f} s but the connection kept failing"
                   + (f" ({connect_error})" if connect_error else ""))
        raise TVUnavailable(f"The TV didn't turn on within {timeout:.0f} s: {why}.")

    async def close(self) -> None:
        await self._drop_client()
