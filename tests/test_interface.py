import time
import pytest
from unittest.mock import MagicMock, patch
from rns_meshcore_interface.chunking import ChunkEncoder


class FakeMeshCoreForInterface:
    """Minimal mock for the MeshCore client used by transport."""
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
        self.adverts_sent = []
        self.commands = self._Commands(self)

    class _Commands:
        def __init__(self, parent):
            self._parent = parent
            self.path_changes = []
            self._contacts = []

        async def send_msg_with_retry(self, dst, msg, **kwargs):
            self._parent.sent_messages.append((dst, msg, kwargs))
            from meshcore.events import EventType, Event
            return Event(EventType.MSG_SENT, {
                "type": 0,
                "expected_ack": b"\x00\x00\x00\x00",
                "suggested_timeout": 5000,
            })

        async def get_contacts(self):
            return self._contacts

        async def change_contact_path(self, contact, path):
            self.path_changes.append((contact, path))
            from meshcore.events import EventType, Event
            return Event(EventType.OK, {})

        async def send_advert(self, flood=False):
            self._parent.adverts_sent.append({"flood": flood})
            from meshcore.events import EventType, Event
            return Event(EventType.OK, {})

    def subscribe(self, event_type, callback):
        self._sub_counter += 1
        sub = MagicMock()
        sub.id = self._sub_counter
        self._subscriptions[sub.id] = callback
        return sub

    def unsubscribe(self, sub):
        self._subscriptions.pop(sub.id, None)

    async def start_auto_message_fetching(self):
        pass

    async def stop_auto_message_fetching(self):
        pass

    async def disconnect(self):
        pass


async def interface_factory(**kwargs):
    return FakeMeshCoreForInterface()


def make_interface(name="TestInterface", peer="a1b2c3d4e5f6", **overrides):
    """Create a MeshCoreInterface with mock transport."""
    # Patch RNS to avoid full Reticulum init
    import RNS
    owner = MagicMock()

    config = {
        "name": name,
        "connection_type": "serial",
        "serial_port": "/dev/ttyUSB0",
        "peer_address": peer,
        "tx_delay_ms": "0",  # no delay for tests
        "_meshcore_factory": interface_factory,
    }
    config.update(overrides)

    from rns_meshcore_interface.interface import MeshCoreInterface
    iface = MeshCoreInterface(owner, config)
    return iface, owner


class TestMeshCoreInterface:
    def test_init_and_online(self):
        iface, owner = make_interface()
        assert iface.online
        assert iface.name == "TestInterface"
        assert iface.HW_MTU == 500
        assert iface.bitrate > 0
        iface.detach()

    def test_process_outgoing(self):
        iface, owner = make_interface()
        data = b"test packet data"
        iface.process_outgoing(data)
        assert iface.txb == len(data)
        iface.detach()

    def test_incoming_reassembly(self):
        iface, owner = make_interface()
        # Simulate incoming chunked message
        encoder = ChunkEncoder()
        data = b"incoming test data"
        messages = encoder.encode_packet(data)
        assert len(messages) == 1

        for msg in messages:
            iface._handle_incoming("aabbcc", msg)

        assert iface.rxb == len(data)
        owner.inbound.assert_called_once_with(data, iface)
        iface.detach()

    def test_incoming_multi_chunk(self):
        iface, owner = make_interface()
        encoder = ChunkEncoder()
        data = bytes(range(256)) * 2  # 512 bytes, multiple chunks
        messages = encoder.encode_packet(data)
        assert len(messages) > 1

        for msg in messages:
            iface._handle_incoming("sender1", msg)

        assert iface.rxb == len(data)
        owner.inbound.assert_called_once_with(data, iface)
        iface.detach()

    def test_non_rns_message_ignored(self):
        iface, owner = make_interface()
        iface._handle_incoming("sender", "Hello from MeshCore!")
        assert iface.rxb == 0
        owner.inbound.assert_not_called()
        iface.detach()

    def test_bitrate_calculation(self):
        iface, owner = make_interface()
        # SF7, BW125kHz, CR1 -> raw ~5468 bps, effective ~3990 bps
        assert 1000 < iface.bitrate < 10000
        iface.detach()

    def test_detach(self):
        iface, owner = make_interface()
        assert iface.online
        iface.detach()
        assert not iface.online

    def test_str_representation(self):
        iface, owner = make_interface(name="MyBridge")
        assert str(iface) == "MeshCoreInterface[MyBridge]"
        iface.detach()

    def test_peer_address_validation(self):
        with pytest.raises(ValueError, match="peer_address"):
            make_interface(peer="abc")  # too short

    def test_should_ingress_limit(self):
        iface, owner = make_interface()
        assert iface.should_ingress_limit() is False
        iface.detach()

    def test_mode_parsing(self):
        iface, owner = make_interface(mode="gateway")
        from RNS.Interfaces.Interface import Interface
        assert iface.mode == Interface.MODE_GATEWAY
        iface.detach()

    def test_transport_disconnect_sets_offline(self):
        iface, owner = make_interface()
        assert iface.online
        iface._on_transport_disconnect()
        assert not iface.online
        iface.detach()

    def test_transport_reconnect_sets_online(self):
        iface, owner = make_interface()
        iface._on_transport_disconnect()
        assert not iface.online
        iface._on_transport_reconnect()
        assert iface.online
        iface.detach()

    def test_route_passed_to_transport(self):
        iface, owner = make_interface(route="23,5f,3a")
        assert iface.transport.route == "23,5f,3a"
        iface.detach()

    def test_allow_flood_fallback_false(self):
        iface, owner = make_interface(allow_flood_fallback="false")
        assert iface.transport.allow_flood_fallback is False
        iface.detach()

    def test_allow_flood_fallback_default(self):
        iface, owner = make_interface()
        assert iface.transport.allow_flood_fallback is True
        assert iface.transport.route is None
        iface.detach()

    def test_advert_on_start_passed_to_transport(self):
        iface, owner = make_interface(advert_on_start="false")
        assert iface.transport.advert_on_start is False
        iface.detach()

    def test_advert_interval_passed_to_transport(self):
        iface, owner = make_interface(advert_interval="300")
        assert iface.transport.advert_interval == 300
        iface.detach()

    def test_advert_defaults(self):
        iface, owner = make_interface()
        assert iface.transport.advert_on_start is True
        assert iface.transport.advert_interval == 0
        iface.detach()

    def test_periodic_cleanup_runs(self):
        iface, owner = make_interface()
        # Add a partial reassembly buffer
        iface.reassembly.add_chunk("sender", 99, 0, 2, "dGVzdA==")
        assert len(iface.reassembly._buffers) == 1
        # Force expiry
        for k in iface.reassembly._buffers:
            iface.reassembly._buffers[k]["timestamp"] = 0
        iface._periodic_cleanup()
        assert len(iface.reassembly._buffers) == 0
        iface.detach()
