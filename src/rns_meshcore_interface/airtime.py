import math
import time
import threading


class AirtimeController:
    """Enforces TX delay between transmissions and tracks duty cycle."""

    def __init__(self, tx_delay_ms=500, max_airtime_percent=0):
        self.tx_delay_ms = tx_delay_ms
        self.max_airtime_percent = max_airtime_percent
        self._last_tx_time = 0.0
        self._tx_airtime_total = 0.0  # total seconds spent transmitting
        self._tracking_start = time.time()
        self._lock = threading.Lock()

    def wait_for_tx_slot(self):
        """Block until tx_delay_ms has elapsed since the last transmission."""
        with self._lock:
            now = time.time()
            elapsed_ms = (now - self._last_tx_time) * 1000
            remaining_ms = self.tx_delay_ms - elapsed_ms
            if remaining_ms > 0:
                time.sleep(remaining_ms / 1000.0)

    def record_tx(self, airtime_seconds=0.0):
        """Record a transmission. Call after sending."""
        with self._lock:
            self._last_tx_time = time.time()
            self._tx_airtime_total += airtime_seconds

    def can_transmit(self):
        """Check if duty cycle limit allows transmission."""
        if self.max_airtime_percent <= 0:
            return True
        with self._lock:
            elapsed = time.time() - self._tracking_start
            if elapsed <= 0:
                return True
            current_percent = (self._tx_airtime_total / elapsed) * 100
            return current_percent < self.max_airtime_percent

    def current_duty_cycle(self):
        """Return current TX duty cycle as a percentage."""
        with self._lock:
            elapsed = time.time() - self._tracking_start
            if elapsed <= 0:
                return 0.0
            return (self._tx_airtime_total / elapsed) * 100

    @staticmethod
    def estimate_airtime_seconds(payload_bytes, sf=7, bw=125000, cr=1):
        """Estimate LoRa time-on-air for a given payload size.

        Args:
            payload_bytes: Number of payload bytes
            sf: Spreading factor (7-12)
            bw: Bandwidth in Hz
            cr: Coding rate denominator minus 4 (1-4, i.e., 4/5 to 4/8)

        Returns:
            Estimated airtime in seconds
        """
        # Symbol duration
        t_sym = (2 ** sf) / bw

        # Preamble (default 8 symbols + 4.25)
        t_preamble = (8 + 4.25) * t_sym

        # Payload symbol count (LoRa formula)
        # DE = 1 for SF >= 11, else 0; IH = 0 (explicit header)
        de = 1 if sf >= 11 else 0
        numerator = 8 * payload_bytes - 4 * sf + 28 + 16  # +16 for CRC
        denominator = 4 * (sf - 2 * de)
        n_payload = 8 + max(math.ceil(numerator / denominator) * (cr + 4), 0)

        t_payload = n_payload * t_sym
        return t_preamble + t_payload

    @staticmethod
    def effective_bitrate(raw_bitrate, tx_delay_ms, encoding_overhead=1.37):
        """Calculate effective bitrate accounting for TX delays and base64 overhead.

        Args:
            raw_bitrate: Raw LoRa bitrate in bps
            tx_delay_ms: Minimum TX delay between messages in ms
            encoding_overhead: Base64 + header overhead factor (default ~1.37x)

        Returns:
            Effective bitrate in bps
        """
        if raw_bitrate <= 0:
            return 0
        # The effective rate is reduced by encoding overhead
        return raw_bitrate / encoding_overhead
