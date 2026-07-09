"""
Hermes Stick Buddy — BLE Central Daemon (runs on Windows).

Polls the VPS-side aggregation server over Tailscale HTTPS,
then sends the JSON heartbeat to the M5StickC Plus over BLE
using the Nordic UART Service (NUS) protocol from REFERENCE.md.

Prerequisites:
    pip install bleak requests

Usage:
    python ble_central.py --url https://your-vps.tailnet:9120 --token YOUR_TOKEN

    Or with config.yaml:
    python ble_central.py --config config.yaml
"""

import argparse
import json
import logging
import time
import sys
import os
import yaml
import asyncio
import struct
from typing import Optional

try:
    from bleak import BleakClient, BleakScanner
except ImportError:
    print("ERROR: bleak not installed. Run: pip install bleak")
    sys.exit(1)

try:
    import requests
except ImportError:
    print("ERROR: requests not installed. Run: pip install requests")
    sys.exit(1)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s: %(message)s",
)
logger = logging.getLogger("ble-central")

# Nordic UART Service UUIDs (from REFERENCE.md)
NUS_SERVICE_UUID = "6e400001-b5a3-f393-e0a9-e50e24dcca9e"
NUS_RX_CHAR_UUID = "6e400002-b5a3-f393-e0a9-e50e24dcca9e"  # desktop → device (write)
NUS_TX_CHAR_UUID = "6e400003-b5a3-f393-e0a9-e50e24dcca9e"  # device → desktop (notify)

# Device filter — the stick advertises a name starting with "Claude"
DEVICE_NAME_PREFIX = "Claude"

# Heartbeat send interval (seconds)
HEARTBEAT_INTERVAL = 10
# Aggressive poll interval when waiting for approval (seconds)
FAST_POLL_INTERVAL = 2
# Normal poll interval (seconds)
NORMAL_POLL_INTERVAL = 5


class VpsClient:
    """Polls the VPS-side aggregation server for heartbeat data."""

    def __init__(self, base_url: str, token: str = "", verify_ssl: bool = True):
        self.base_url = base_url.rstrip("/")
        self.session = requests.Session()
        self.session.headers.update({"Accept": "application/json"})
        if token:
            self.session.headers.update({"Authorization": f"Bearer {token}"})
        self.session.verify = verify_ssl

    def get_heartbeat(self) -> Optional[dict]:
        """Fetch the heartbeat JSON from the VPS server."""
        try:
            url = f"{self.base_url}/heartbeat"
            resp = self.session.get(url, timeout=10)
            if resp.status_code == 200:
                return resp.json()
            logger.warning(f"Heartbeat fetch failed: HTTP {resp.status_code}")
            return None
        except Exception as e:
            logger.warning(f"Heartbeat fetch error: {e}")
            return None

    def get_health(self) -> bool:
        """Check if the VPS server is reachable."""
        try:
            resp = self.session.get(f"{self.base_url}/health", timeout=5)
            return resp.status_code == 200
        except Exception:
            return False


class BleStickClient:
    """BLE client for the M5StickC Plus running the buddy firmware."""

    def __init__(self, device_name_prefix: str = DEVICE_NAME_PREFIX):
        self.device_name_prefix = device_name_prefix
        self.client: Optional[BleakClient] = None
        self.device = None
        self._connected = False
        self._rx_buf = bytearray()

    async def scan(self, timeout: float = 10.0) -> Optional[object]:
        """Scan for the stick device. Returns the device or None."""
        logger.info(f"Scanning for BLE device starting with '{self.device_name_prefix}'...")

        devices = await BleakScanner.discover(timeout=timeout)

        for device in devices:
            name = device.name or ""
            if name.startswith(self.device_name_prefix):
                logger.info(f"Found device: {name} ({device.address})")
                self.device = device
                return device

        logger.warning(f"No device found starting with '{self.device_name_prefix}'")
        return None

    async def connect(self, max_retries: int = 3) -> bool:
        """Connect to the stick and subscribe to TX notifications."""
        if self._connected and self.client and self.client.is_connected:
            return True

        if not self.device:
            found = await self.scan()
            if not found:
                return False

        for attempt in range(max_retries):
            try:
                assert self.device is not None, "No device to connect to"
                logger.info(
                    f"Connecting to {self.device.name} (attempt {attempt + 1})..."
                )
                self.client = BleakClient(
                    self.device.address,
                    disconnected_callback=self._on_disconnect,
                )
                await self.client.connect()
                await self.client.start_notify(NUS_TX_CHAR_UUID, self._on_tx_notify)
                self._connected = True
                logger.info(f"Connected to {self.device.name}")
                return True
            except Exception as e:
                logger.warning(f"Connect attempt {attempt + 1} failed: {e}")
                await asyncio.sleep(2)

        return False

    def _on_disconnect(self, client: BleakClient):
        logger.warning("BLE disconnected")
        self._connected = False

    def _on_tx_notify(self, sender, data: bytearray):
        """Handle notifications from the stick (approval decisions, acks, etc.)."""
        self._rx_buf.extend(data)
        # Process complete lines
        while b"\n" in self._rx_buf:
            idx = self._rx_buf.index(b"\n")
            line = self._rx_buf[:idx].decode("utf-8", errors="replace").strip()
            self._rx_buf = self._rx_buf[idx + 1 :]
            if line:
                self._handle_stick_message(line)

    def _handle_stick_message(self, line: str):
        """Handle a JSON message from the stick (e.g., approval decision, ack)."""
        if not line.startswith("{"):
            return
        try:
            msg = json.loads(line)
            cmd = msg.get("cmd")
            if cmd == "permission":
                # Approval decision from the stick
                decision = msg.get("decision")
                prompt_id = msg.get("id")
                logger.info(f"Approval decision from stick: {decision} for {prompt_id}")
                # TODO: Forward to Hermes via VPS API
                # For now, just log it
            elif msg.get("ack"):
                ack_type = msg.get("ack")
                logger.debug(f"Stick ack: {ack_type} ok={msg.get('ok')}")
            else:
                logger.debug(f"Stick message: {msg}")
        except json.JSONDecodeError:
            logger.debug(f"Non-JSON from stick: {line}")

    async def send_json(self, data: dict) -> bool:
        """Send a JSON object as a newline-terminated line over NUS RX."""
        if not self._connected or not self.client or not self.client.is_connected:
            logger.warning("Not connected, cannot send")
            return False

        try:
            line = json.dumps(data) + "\n"
            # NUS RX characteristic has a max payload — split into chunks
            payload = line.encode("utf-8")
            chunk_size = 180  # Safe BLE write size (MTU - 3 header bytes)

            for i in range(0, len(payload), chunk_size):
                chunk = payload[i : i + chunk_size]
                await self.client.write_gatt_char(NUS_RX_CHAR_UUID, chunk)

            logger.debug(f"Sent heartbeat: {data.get('msg', '?')}")
            return True
        except Exception as e:
            logger.warning(f"Send failed: {e}")
            self._connected = False
            return False

    async def send_time_sync(self):
        """Send initial time sync to the stick."""
        if not self._connected:
            return
        import datetime
        now = datetime.datetime.now()
        epoch = int(now.timestamp())
        # Calculate timezone offset in seconds
        tz_offset = -time.timezone if time.daylight == 0 else -time.altzone
        await self.send_json({"time": [epoch, tz_offset]})

    async def disconnect(self):
        if self.client and self.client.is_connected:
            await self.client.disconnect()
        self._connected = False

    @property
    def connected(self) -> bool:
        return self._connected


async def run_daemon(
    vps_url: str,
    token: str = "",
    verify_ssl: bool = True,
    poll_interval: float = NORMAL_POLL_INTERVAL,
):
    """Main daemon loop: poll VPS → send to stick over BLE."""
    vps = VpsClient(vps_url, token, verify_ssl)
    stick = BleStickClient()

    # Wait for VPS to be reachable
    logger.info(f"Connecting to VPS at {vps_url}...")
    retries = 0
    while not vps.get_health():
        retries += 1
        if retries > 10:
            logger.error("Cannot reach VPS server, exiting")
            return
        logger.warning(f"VPS not reachable (attempt {retries}/10), retrying...")
        await asyncio.sleep(5)

    logger.info("VPS server reachable")

    # Connect to the stick
    if not await stick.connect():
        logger.error("Cannot connect to stick, retrying in background...")
        # Continue anyway — we'll retry connecting in the loop

    # Send initial time sync
    await stick.send_time_sync()

    consecutive_failures = 0
    max_consecutive_failures = 5

    while True:
        try:
            # Fetch heartbeat from VPS
            heartbeat = vps.get_heartbeat()
            if heartbeat is None:
                consecutive_failures += 1
                if consecutive_failures > max_consecutive_failures:
                    logger.error("Too many VPS failures, backing off 30s")
                    await asyncio.sleep(30)
                    consecutive_failures = 0
                else:
                    await asyncio.sleep(poll_interval)
                continue

            consecutive_failures = 0

            # Ensure BLE connection
            if not stick.connected:
                logger.info("Reconnecting to stick...")
                if not await stick.connect(max_retries=1):
                    logger.warning("Stick reconnection failed, will retry next cycle")
                    await asyncio.sleep(poll_interval)
                    continue
                await stick.send_time_sync()

            # Send heartbeat to stick
            success = await stick.send_json(heartbeat)
            if not success:
                logger.warning("Heartbeat send failed, will reconnect next cycle")
            else:
                logger.info(
                    f"Heartbeat sent: {heartbeat.get('msg', '?')} | "
                    f"tokens={heartbeat.get('tokens_today', 0):,}"
                )

            # Adjust poll interval based on waiting state
            if heartbeat.get("waiting", 0) > 0:
                await asyncio.sleep(FAST_POLL_INTERVAL)
            else:
                await asyncio.sleep(poll_interval)

        except KeyboardInterrupt:
            logger.info("Shutting down...")
            break
        except Exception as e:
            logger.error(f"Unexpected error in main loop: {e}")
            await asyncio.sleep(poll_interval)

    await stick.disconnect()


def main():
    parser = argparse.ArgumentParser(
        description="Hermes Stick Buddy BLE Central Daemon"
    )
    parser.add_argument(
        "--url",
        default=os.environ.get("STICK_BUDDY_URL", ""),
        help="VPS server URL (e.g., https://your-vps.tailnet:9120)",
    )
    parser.add_argument(
        "--token",
        default=os.environ.get("STICK_BUDDY_TOKEN", ""),
        help="Auth bearer token",
    )
    parser.add_argument(
        "--config",
        default=None,
        help="Path to config.yaml (alternative to --url/--token)",
    )
    parser.add_argument(
        "--no-verify-ssl",
        action="store_true",
        help="Disable SSL certificate verification (not recommended)",
    )
    parser.add_argument(
        "--poll-interval",
        type=float,
        default=NORMAL_POLL_INTERVAL,
        help="Poll interval in seconds (default: 5)",
    )
    parser.add_argument(
        "--debug", action="store_true", help="Enable debug logging"
    )

    args = parser.parse_args()

    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)

    # Load config if provided
    if args.config:
        with open(args.config) as f:
            cfg = yaml.safe_load(f)
        url = cfg.get("vps_url", args.url)
        token = cfg.get("token", args.token)
        verify_ssl = cfg.get("verify_ssl", not args.no_verify_ssl)
        poll_interval = cfg.get("poll_interval", args.poll_interval)
    else:
        url = args.url
        token = args.token
        verify_ssl = not args.no_verify_ssl
        poll_interval = args.poll_interval

    if not url:
        print("ERROR: --url is required (or set STICK_BUDDY_URL env var)")
        sys.exit(1)

    logger.info(f"Starting BLE Central Daemon")
    logger.info(f"VPS URL: {url}")
    logger.info(f"SSL verify: {verify_ssl}")
    logger.info(f"Poll interval: {poll_interval}s")

    asyncio.run(run_daemon(url, token, verify_ssl, poll_interval))


if __name__ == "__main__":
    main()