import RNS
import time
import threading
from RNS.Interfaces.Interface import Interface

from .chunking import ChunkEncoder, ReassemblyBuffer, DEFAULT_MAX_MSG_LEN
from .airtime import AirtimeController
from .transport import MeshCoreTransport


class MeshCoreInterface(Interface):
    DEFAULT_IFAC_SIZE = 8

    # Mode name -> mode constant mapping
    MODES = {
        "full": Interface.MODE_FULL,
        "point_to_point": Interface.MODE_POINT_TO_POINT,
        "access_point": Interface.MODE_ACCESS_POINT,
        "roaming": Interface.MODE_ROAMING,
        "boundary": Interface.MODE_BOUNDARY,
        "gateway": Interface.MODE_GATEWAY,
    }

    def __init__(self, owner, configuration):
        super().__init__()

        ifconf = Interface.get_config_obj(configuration)
        self.name = ifconf["name"]
        self.owner = owner
        self.online = False
        self.HW_MTU = 500

        # Parse config
        connection_type = ifconf.get("connection_type", "serial")
        serial_port = ifconf.get("serial_port", None)
        serial_baudrate = int(ifconf.get("serial_baudrate", 115200))
        tcp_host = ifconf.get("tcp_host", None)
        tcp_port = int(ifconf.get("tcp_port", 5555))
        peer_address = ifconf.get("peer_address", None)
        max_msg_len = int(ifconf.get("max_msg_len", DEFAULT_MAX_MSG_LEN))
        tx_delay_ms = int(ifconf.get("tx_delay_ms", 500))
        max_airtime_percent = float(ifconf.get("max_airtime_percent", 0))
        path = ifconf.get("path", None)
        allow_flood_fallback = str(ifconf.get("allow_flood_fallback", "true")).lower() == "true"
        max_retries = int(ifconf.get("max_retries", 3))
        max_flood_retries = int(ifconf.get("max_flood_retries", 2))
        multi_acks_raw = ifconf.get("multi_acks", None)
        multi_acks = int(multi_acks_raw) if multi_acks_raw is not None else None
        advert_on_start = str(ifconf.get("advert_on_start", "true")).lower() == "true"
        advert_interval = int(ifconf.get("advert_interval", 0))

        if peer_address is None or len(peer_address) < 12:
            raise ValueError(
                f"peer_address must be at least 12 hex chars (6 bytes), got: {peer_address}"
            )

        # Parse mode
        mode_str = ifconf.get("mode", "gateway")
        if mode_str in self.MODES:
            self.mode = self.MODES[mode_str]
        else:
            self.mode = Interface.MODE_GATEWAY

        # Initialize components
        self.chunker = ChunkEncoder(max_msg_len=max_msg_len)
        self.reassembly = ReassemblyBuffer()
        self.airtime = AirtimeController(
            tx_delay_ms=tx_delay_ms,
            max_airtime_percent=max_airtime_percent,
        )

        # Allow DI for testing
        meshcore_factory = ifconf.get("_meshcore_factory", None)

        self.transport = MeshCoreTransport(
            connection_type=connection_type,
            serial_port=serial_port,
            serial_baudrate=serial_baudrate,
            tcp_host=tcp_host,
            tcp_port=tcp_port,
            peer_address=peer_address,
            meshcore_factory=meshcore_factory,
            path=path,
            allow_flood_fallback=allow_flood_fallback,
            max_retries=max_retries,
            max_flood_retries=max_flood_retries,
            multi_acks=multi_acks,
            advert_on_start=advert_on_start,
            advert_interval=advert_interval,
        )
        self.transport.on_message = self._handle_incoming
        self.transport.on_disconnect = self._on_transport_disconnect
        self.transport.on_reconnect = self._on_transport_reconnect

        try:
            self.transport.start()
        except Exception as e:
            RNS.log(
                f"Could not connect MeshCore transport for {self}: {e}",
                RNS.LOG_ERROR,
            )
            raise

        # Calculate bitrate from radio params
        self._update_bitrate()

        # Start periodic reassembly cleanup
        self._cleanup_interval = 60  # seconds
        self._cleanup_timer = None
        self._start_cleanup_timer()

        self.online = True
        RNS.log(f"{self} is now online (bitrate: {self.bitrate} bps)", RNS.LOG_NOTICE)

    def _update_bitrate(self):
        """Calculate and set bitrate from MeshCore radio parameters."""
        params = self.transport.radio_params
        sf = params.get("radio_sf", 7)
        bw = params.get("radio_bw", 125.0)
        cr = params.get("radio_cr", 1)

        bw_hz = bw * 1000 if bw < 1000 else bw
        symbol_rate = bw_hz / (2 ** sf)
        raw_bitrate = sf * (4.0 / (4 + cr)) * symbol_rate
        self.bitrate = int(
            AirtimeController.effective_bitrate(
                raw_bitrate, self.airtime.tx_delay_ms
            )
        )

    def process_outgoing(self, data):
        if not self.online:
            return

        if not self.airtime.can_transmit():
            RNS.log(f"{self} duty cycle limit reached, dropping packet", RNS.LOG_WARNING)
            return

        try:
            chunks = self.chunker.encode_packet(data)
        except ValueError as e:
            RNS.log(f"{self} packet too large: {e}", RNS.LOG_ERROR)
            return

        for chunk_msg in chunks:
            self.airtime.wait_for_tx_slot()
            success = self.transport.send_message(chunk_msg)
            if success:
                airtime = AirtimeController.estimate_airtime_seconds(
                    len(chunk_msg),
                    sf=self.transport.radio_params.get("radio_sf", 7),
                    bw=self.transport.radio_params.get("radio_bw", 125000),
                    cr=self.transport.radio_params.get("radio_cr", 1),
                )
                self.airtime.record_tx(airtime)
                self.txb += len(data) if len(chunks) == 1 else 0
            else:
                RNS.log(f"{self} failed to send chunk", RNS.LOG_WARNING)
                return

        # Count TX bytes once for the whole packet
        if len(chunks) > 1:
            self.txb += len(data)

    def _handle_incoming(self, sender, text):
        """Called from transport thread when a MeshCore message arrives."""
        if not ChunkEncoder.is_rns_message(text):
            return

        parsed = ChunkEncoder.parse_chunk(text)
        if parsed is None:
            return

        msg_id, chunk_idx, total, b64_fragment = parsed
        packet_data = self.reassembly.add_chunk(sender, msg_id, chunk_idx, total, b64_fragment)

        if packet_data is not None:
            self.rxb += len(packet_data)
            self.owner.inbound(packet_data, self)

    def _on_transport_disconnect(self):
        self.online = False
        RNS.log(f"{self} lost connection, attempting reconnection...", RNS.LOG_WARNING)

    def _on_transport_reconnect(self):
        self._update_bitrate()
        self.online = True
        RNS.log(f"{self} reconnected", RNS.LOG_NOTICE)

    def _start_cleanup_timer(self):
        if self._cleanup_timer:
            self._cleanup_timer.cancel()
        self._cleanup_timer = threading.Timer(
            self._cleanup_interval, self._periodic_cleanup
        )
        self._cleanup_timer.daemon = True
        self._cleanup_timer.start()

    def _periodic_cleanup(self):
        try:
            removed = self.reassembly.cleanup_expired()
            if removed > 0:
                RNS.log(f"{self} cleaned up {removed} expired reassembly buffers", RNS.LOG_DEBUG)
        except Exception:
            pass
        if self.online:
            self._start_cleanup_timer()

    def detach(self):
        self.online = False
        if self._cleanup_timer:
            self._cleanup_timer.cancel()
        self.transport.stop()
        RNS.log(f"{self} detached", RNS.LOG_NOTICE)

    def should_ingress_limit(self):
        return False

    def __str__(self):
        return f"MeshCoreInterface[{self.name}]"
