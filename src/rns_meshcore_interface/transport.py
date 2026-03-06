import asyncio
import logging
import threading
import time

log = logging.getLogger(__name__)


class MeshCoreTransport:
    """Bridges sync Reticulum calls to async meshcore_py via a dedicated event loop thread.

    Args:
        connection_type: "serial" or "tcp"
        serial_port: Serial port path (for serial connections)
        serial_baudrate: Baud rate (for serial connections)
        tcp_host: TCP host (for tcp connections)
        tcp_port: TCP port (for tcp connections)
        peer_address: Hex public key prefix of the peer MeshCore node
        meshcore_factory: Optional callable returning a MeshCore instance (for testing/DI).
                          If None, uses the real meshcore library.
    """

    def __init__(
        self,
        connection_type="serial",
        serial_port=None,
        serial_baudrate=115200,
        tcp_host=None,
        tcp_port=5555,
        peer_address=None,
        meshcore_factory=None,
        route=None,
        allow_flood_fallback=True,
        max_retries=3,
        max_flood_retries=2,
        advert_on_start=True,
        advert_interval=0,
    ):
        self.connection_type = connection_type
        self.serial_port = serial_port
        self.serial_baudrate = serial_baudrate
        self.tcp_host = tcp_host
        self.tcp_port = tcp_port
        self.peer_address = peer_address
        self._meshcore_factory = meshcore_factory
        self.route = route
        self.allow_flood_fallback = allow_flood_fallback
        self.max_retries = max_retries
        self.max_flood_retries = max_flood_retries
        self.advert_on_start = advert_on_start
        self.advert_interval = advert_interval

        self._loop = None
        self._thread = None
        self._mc = None
        self._subscription = None
        self._advert_task = None
        self._is_connected = False
        self._stopping = False
        self._radio_params = {}
        self.on_message = None  # callback: on_message(sender, text)
        self.on_disconnect = None  # callback: on_disconnect()
        self.on_reconnect = None  # callback: on_reconnect()

    @property
    def is_connected(self):
        return self._is_connected

    @property
    def radio_params(self):
        return dict(self._radio_params)

    def start(self):
        """Start the asyncio event loop in a daemon thread and connect."""
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()

        # Block until connection is established (or fails)
        future = asyncio.run_coroutine_threadsafe(self._connect(), self._loop)
        future.result(timeout=30)

    def stop(self):
        """Disconnect and stop the event loop."""
        self._stopping = True
        if self._loop and self._loop.is_running():
            future = asyncio.run_coroutine_threadsafe(self._disconnect(), self._loop)
            try:
                future.result(timeout=10)
            except Exception:
                pass
            self._loop.call_soon_threadsafe(self._loop.stop)
        if self._thread:
            self._thread.join(timeout=5)
        self._is_connected = False

    def send_message(self, text: str) -> bool:
        """Send a message to the peer node. Blocks until ACK or retry exhaustion."""
        if not self._is_connected or not self._loop:
            return False
        try:
            future = asyncio.run_coroutine_threadsafe(
                self._send_msg(text), self._loop
            )
            result = future.result(timeout=120)
            return result
        except Exception as e:
            log.error(f"Send failed: {e}")
            return False

    def _run_loop(self):
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    async def _connect(self):
        try:
            if self._meshcore_factory:
                self._mc = await self._meshcore_factory(
                    connection_type=self.connection_type,
                    serial_port=self.serial_port,
                    serial_baudrate=self.serial_baudrate,
                    tcp_host=self.tcp_host,
                    tcp_port=self.tcp_port,
                )
            else:
                from meshcore import MeshCore

                if self.connection_type == "tcp":
                    self._mc = await MeshCore.create_tcp(
                        host=self.tcp_host,
                        port=self.tcp_port,
                    )
                else:
                    self._mc = await MeshCore.create_serial(
                        port=self.serial_port,
                        baudrate=self.serial_baudrate,
                    )

            # Subscribe to incoming messages
            from meshcore.events import EventType

            self._subscription = self._mc.subscribe(
                EventType.CONTACT_MSG_RECV, self._on_incoming_message
            )

            # Fetch device info for radio params
            self._radio_params = dict(self._mc.self_info) if self._mc.self_info else {}

            # Start auto-fetching so incoming messages are retrieved from device
            await self._mc.start_auto_message_fetching()

            self._is_connected = True
            log.info("MeshCore transport connected")

            # Send flood advert on startup if configured
            # (before route, so path updates don't overwrite the manual route)
            if self.advert_on_start:
                try:
                    await self._mc.commands.send_advert(flood=True)
                    log.info("Sent flood advert on startup")
                except Exception as e:
                    log.warning(f"Failed to send startup flood advert: {e}")

            # Apply manual route if configured (after advert)
            if self.route:
                await self._apply_route()

            # Start periodic flood advert if configured
            if self.advert_interval > 0:
                self._advert_task = asyncio.ensure_future(self._periodic_advert())

        except Exception as e:
            log.error(f"MeshCore connection failed: {e}")
            self._is_connected = False
            raise

    async def _periodic_advert(self):
        """Periodically send flood advertisements."""
        while not self._stopping and self._is_connected:
            try:
                await asyncio.sleep(self.advert_interval)
                if self._stopping or not self._is_connected:
                    break
                await self._mc.commands.send_advert(flood=True)
                log.info("Sent periodic flood advert")
                if self.route:
                    await self._apply_route()
            except asyncio.CancelledError:
                break
            except Exception as e:
                log.warning(f"Periodic flood advert failed: {e}")

    async def _disconnect(self):
        try:
            if self._advert_task:
                self._advert_task.cancel()
                self._advert_task = None
            if self._subscription:
                self._mc.unsubscribe(self._subscription)
                self._subscription = None
            if self._mc:
                await self._mc.stop_auto_message_fetching()
                await self._mc.disconnect()
                self._mc = None
        except Exception as e:
            log.warning(f"Error during disconnect: {e}")
        self._is_connected = False

    async def _apply_route(self):
        """Set the manual route on the peer contact."""
        try:
            contacts = await self._mc.commands.get_contacts()
            contact = None
            for c in contacts:
                pk = c.get("public_key", "")
                if pk.startswith(self.peer_address) or self.peer_address.startswith(pk):
                    contact = c
                    break
            if contact is None:
                log.warning(
                    f"Peer {self.peer_address} not found in contacts, "
                    "cannot apply manual route"
                )
                return
            await self._mc.commands.change_contact_path(contact, self.route)
            log.info(f"Applied manual route: {self.route}")
        except Exception as e:
            log.error(f"Failed to apply manual route: {e}")

    async def _send_msg(self, text: str) -> bool:
        if not self._mc:
            return False
        try:
            flood_retries = 0 if not self.allow_flood_fallback else self.max_flood_retries
            # If flood is disabled, set flood_after beyond max_attempts
            # to prevent send_msg_with_retry from resetting the path
            flood_after = 2 if flood_retries > 0 else self.max_retries + 1
            result = await self._mc.commands.send_msg_with_retry(
                self.peer_address, text,
                max_attempts=self.max_retries,
                max_flood_attempts=flood_retries,
                flood_after=flood_after,
            )
            return result is not None
        except Exception as e:
            log.error(f"send_msg error: {e}")
            self._handle_connection_lost()
            return False

    def _handle_connection_lost(self):
        """Trigger reconnection when connection is lost."""
        if self._is_connected and not self._stopping:
            self._is_connected = False
            if self.on_disconnect:
                self.on_disconnect()
            if self._loop and self._loop.is_running():
                asyncio.run_coroutine_threadsafe(self._reconnect_loop(), self._loop)

    async def _reconnect_loop(self):
        """Attempt reconnection with exponential backoff."""
        base_delay = 5
        max_delay = 300
        delay = base_delay

        while not self._stopping and not self._is_connected:
            log.info(f"Attempting reconnection in {delay}s...")
            await asyncio.sleep(delay)
            if self._stopping:
                break
            try:
                await self._disconnect()
                await self._connect()
                log.info("Reconnected successfully")
                if self.on_reconnect:
                    self.on_reconnect()
                break
            except Exception as e:
                log.warning(f"Reconnection failed: {e}")
                delay = min(delay * 2, max_delay)

    async def _on_incoming_message(self, event):
        """Handle incoming MeshCore message events."""
        try:
            sender = event.payload.get("pubkey_prefix", "unknown")
            text = event.payload.get("text", "")
            if self.on_message:
                self.on_message(sender, text)
        except Exception as e:
            log.error(f"Error handling incoming message: {e}")
