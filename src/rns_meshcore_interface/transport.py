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
    ):
        self.connection_type = connection_type
        self.serial_port = serial_port
        self.serial_baudrate = serial_baudrate
        self.tcp_host = tcp_host
        self.tcp_port = tcp_port
        self.peer_address = peer_address
        self._meshcore_factory = meshcore_factory

        self._loop = None
        self._thread = None
        self._mc = None
        self._subscription = None
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
        """Send a message to the peer node. Blocks until sent (fire-and-forget, no ACK wait)."""
        if not self._is_connected or not self._loop:
            return False
        try:
            future = asyncio.run_coroutine_threadsafe(
                self._send_msg(text), self._loop
            )
            result = future.result(timeout=15)
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

            self._is_connected = True
            log.info("MeshCore transport connected")

        except Exception as e:
            log.error(f"MeshCore connection failed: {e}")
            self._is_connected = False
            raise

    async def _disconnect(self):
        try:
            if self._subscription:
                self._mc.unsubscribe(self._subscription)
                self._subscription = None
            if self._mc:
                await self._mc.disconnect()
                self._mc = None
        except Exception as e:
            log.warning(f"Error during disconnect: {e}")
        self._is_connected = False

    async def _send_msg(self, text: str) -> bool:
        if not self._mc:
            return False
        try:
            result = await self._mc.commands.send_msg(self.peer_address, text)
            from meshcore.events import EventType

            return result.type == EventType.MSG_SENT
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
