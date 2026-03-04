import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from rns_meshcore_interface.transport import MeshCoreTransport


class FakeMeshCore:
    """Mock MeshCore client for testing."""

    def __init__(self):
        self.self_info = {
            "radio_freq": 915.0,
            "radio_bw": 125.0,
            "radio_sf": 7,
            "radio_cr": 1,
        }
        self._subscriptions = {}
        self._sub_counter = 0
        self.sent_messages = []
        self.disconnected = False

    def subscribe(self, event_type, callback):
        self._sub_counter += 1
        sub_id = self._sub_counter
        self._subscriptions[sub_id] = (event_type, callback)
        sub = MagicMock()
        sub.id = sub_id
        return sub

    def unsubscribe(self, sub):
        self._subscriptions.pop(sub.id, None)

    async def send_msg(self, dst, msg):
        self.sent_messages.append((dst, msg))
        event = MagicMock()
        event.type = MagicMock()
        event.type.name = "MSG_SENT"
        # Make it match EventType.MSG_SENT
        from meshcore.events import EventType
        event.type = EventType.MSG_SENT
        return event

    async def disconnect(self):
        self.disconnected = True


async def fake_factory(**kwargs):
    return FakeMeshCore()


class TestMeshCoreTransport:
    def test_start_and_stop(self):
        transport = MeshCoreTransport(
            connection_type="serial",
            serial_port="/dev/ttyUSB0",
            peer_address="a1b2c3d4e5f6",
            meshcore_factory=fake_factory,
        )
        transport.start()
        assert transport.is_connected
        assert transport.radio_params.get("radio_sf") == 7
        transport.stop()
        assert not transport.is_connected

    def test_send_message(self):
        transport = MeshCoreTransport(
            connection_type="serial",
            serial_port="/dev/ttyUSB0",
            peer_address="a1b2c3d4e5f6",
            meshcore_factory=fake_factory,
        )
        transport.start()
        result = transport.send_message("RNS|00|0|1|dGVzdA==")
        assert result is True
        transport.stop()

    def test_send_when_disconnected(self):
        transport = MeshCoreTransport(
            peer_address="a1b2c3d4e5f6",
            meshcore_factory=fake_factory,
        )
        # Don't start - should fail
        result = transport.send_message("test")
        assert result is False

    def test_on_message_callback(self):
        transport = MeshCoreTransport(
            peer_address="a1b2c3d4e5f6",
            meshcore_factory=fake_factory,
        )
        received = []
        transport.on_message = lambda sender, text: received.append((sender, text))
        transport.start()

        # Simulate an incoming message by calling the internal handler
        event = MagicMock()
        event.payload = {"pubkey_prefix": "aabbcc", "text": "RNS|00|0|1|dGVzdA=="}
        future = asyncio.run_coroutine_threadsafe(
            transport._on_incoming_message(event), transport._loop
        )
        future.result(timeout=5)

        assert len(received) == 1
        assert received[0] == ("aabbcc", "RNS|00|0|1|dGVzdA==")
        transport.stop()

    def test_radio_params(self):
        transport = MeshCoreTransport(
            peer_address="a1b2c3d4e5f6",
            meshcore_factory=fake_factory,
        )
        transport.start()
        params = transport.radio_params
        assert params["radio_freq"] == 915.0
        assert params["radio_bw"] == 125.0
        assert params["radio_sf"] == 7
        assert params["radio_cr"] == 1
        transport.stop()

    def test_connection_failure(self):
        async def failing_factory(**kwargs):
            raise ConnectionError("Cannot connect")

        transport = MeshCoreTransport(
            peer_address="a1b2c3d4e5f6",
            meshcore_factory=failing_factory,
        )
        with pytest.raises(ConnectionError):
            transport.start()
        assert not transport.is_connected
        transport.stop()

    def test_disconnect_callback(self):
        transport = MeshCoreTransport(
            peer_address="a1b2c3d4e5f6",
            meshcore_factory=fake_factory,
        )
        disconnected = []
        transport.on_disconnect = lambda: disconnected.append(True)
        transport.start()
        assert transport.is_connected

        # Simulate connection loss
        transport._handle_connection_lost()
        assert not transport.is_connected
        assert len(disconnected) == 1
        transport.stop()

    def test_stopping_prevents_reconnect(self):
        transport = MeshCoreTransport(
            peer_address="a1b2c3d4e5f6",
            meshcore_factory=fake_factory,
        )
        transport.start()
        transport._stopping = True
        transport._handle_connection_lost()
        # Should not start reconnect loop when stopping
        transport.stop()
