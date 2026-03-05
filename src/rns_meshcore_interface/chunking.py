import base64
import logging
import time
import threading

log = logging.getLogger(__name__)

MAGIC_PREFIX = "RNS|"
MAX_CHUNKS = 15
HEADER_OVERHEAD = 14  # "RNS|XX|X|X|"
DEFAULT_MAX_MSG_LEN = 200
DEFAULT_EXPIRY_SECONDS = 120


class ChunkEncoder:
    """Encodes RNS packets into MeshCore-sized chunked messages and parses them back."""

    def __init__(self, max_msg_len=DEFAULT_MAX_MSG_LEN):
        self.max_msg_len = max_msg_len
        self.max_payload_chars = max_msg_len - HEADER_OVERHEAD
        self._counter = 0
        self._lock = threading.Lock()

    def _next_msg_id(self):
        with self._lock:
            msg_id = self._counter
            self._counter = (self._counter + 1) % 256
            return msg_id

    def encode_packet(self, data: bytes) -> list:
        """Encode a raw RNS packet into a list of chunked MeshCore message strings."""
        b64 = base64.urlsafe_b64encode(data).decode("ascii")
        chunk_size = self.max_payload_chars
        chunks = [b64[i:i + chunk_size] for i in range(0, len(b64), chunk_size)]
        total = len(chunks)

        if total > MAX_CHUNKS:
            raise ValueError(
                f"Packet too large: {len(data)} bytes requires {total} chunks (max {MAX_CHUNKS})"
            )

        msg_id = self._next_msg_id()
        messages = []
        for idx, chunk in enumerate(chunks):
            header = f"{MAGIC_PREFIX}{msg_id:02x}|{idx:01x}|{total:01x}|"
            messages.append(header + chunk)

        return messages

    @staticmethod
    def is_rns_message(text: str) -> bool:
        """Check if a MeshCore message is an RNS bridge message."""
        return text.startswith(MAGIC_PREFIX)

    @staticmethod
    def parse_chunk(text: str):
        """Parse a chunked message string into (msg_id, chunk_idx, total, b64_fragment_str) or None.

        The fourth element is a base64 fragment string, NOT decoded bytes.
        Fragments must be concatenated in order before base64 decoding.
        """
        if not text.startswith(MAGIC_PREFIX):
            return None
        try:
            rest = text[len(MAGIC_PREFIX):]
            parts = rest.split("|", 3)
            if len(parts) != 4:
                return None
            msg_id = int(parts[0], 16)
            chunk_idx = int(parts[1], 16)
            total = int(parts[2], 16)
            b64_fragment = parts[3]
            if total < 1 or total > MAX_CHUNKS:
                return None
            return (msg_id, chunk_idx, total, b64_fragment)
        except ValueError:
            return None


class ReassemblyBuffer:
    """Collects chunks from multiple senders and reassembles complete packets."""

    def __init__(self, expiry_seconds=DEFAULT_EXPIRY_SECONDS):
        self.expiry_seconds = expiry_seconds
        self._buffers = {}  # key: (sender, msg_id) -> {chunks: dict, total: int, timestamp: float}
        self._lock = threading.Lock()

    def add_chunk(self, sender: str, msg_id: int, chunk_idx: int, total: int, b64_fragment: str):
        """Add a base64 fragment. Returns the reassembled packet bytes if complete, else None."""
        key = (sender, msg_id)
        with self._lock:
            if key not in self._buffers:
                self._buffers[key] = {
                    "chunks": {},
                    "total": total,
                    "timestamp": time.time(),
                }
            buf = self._buffers[key]
            buf["chunks"][chunk_idx] = b64_fragment
            buf["timestamp"] = time.time()

            if len(buf["chunks"]) == buf["total"]:
                # Reassemble base64 string in order, then decode
                full_b64 = "".join(buf["chunks"][i] for i in range(buf["total"]))
                del self._buffers[key]
                # Restore padding that may be stripped in transit
                padding = 4 - (len(full_b64) % 4)
                if padding < 4:
                    full_b64 += "=" * padding
                try:
                    return base64.urlsafe_b64decode(full_b64)
                except base64.binascii.Error as e:
                    log.error(f"Base64 decode failed for {key}: {e}")
                    return None

        return None

    def cleanup_expired(self):
        """Remove buffers older than expiry_seconds."""
        now = time.time()
        with self._lock:
            expired = [
                k for k, v in self._buffers.items()
                if now - v["timestamp"] > self.expiry_seconds
            ]
            for k in expired:
                del self._buffers[k]
            return len(expired)
