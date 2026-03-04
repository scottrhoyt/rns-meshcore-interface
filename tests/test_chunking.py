import time
import pytest
from rns_meshcore_interface.chunking import (
    ChunkEncoder,
    ReassemblyBuffer,
    MAGIC_PREFIX,
    MAX_CHUNKS,
    DEFAULT_MAX_MSG_LEN,
    HEADER_OVERHEAD,
)


class TestChunkEncoder:
    def setup_method(self):
        self.encoder = ChunkEncoder()

    def test_single_chunk_small_packet(self):
        data = b"hello"
        messages = self.encoder.encode_packet(data)
        assert len(messages) == 1
        assert messages[0].startswith(MAGIC_PREFIX)
        # Verify header format: RNS|XX|X|X|
        parts = messages[0][len(MAGIC_PREFIX):].split("|", 3)
        assert len(parts) == 4
        assert int(parts[1], 16) == 0  # chunk_idx
        assert int(parts[2], 16) == 1  # total

    def test_multi_chunk_large_packet(self):
        # 500 bytes -> base64 is ~668 chars -> needs multiple chunks
        data = bytes(range(256)) + bytes(range(244))
        assert len(data) == 500
        messages = self.encoder.encode_packet(data)
        assert len(messages) > 1
        # Verify all chunks reference same msg_id
        msg_ids = set()
        for msg in messages:
            parsed = ChunkEncoder.parse_chunk(msg)
            assert parsed is not None
            msg_ids.add(parsed[0])
        assert len(msg_ids) == 1

    def test_round_trip_single_chunk(self):
        data = b"test round trip"
        messages = self.encoder.encode_packet(data)
        parsed = ChunkEncoder.parse_chunk(messages[0])
        assert parsed is not None
        msg_id, idx, total, b64_frag = parsed
        assert total == 1
        assert idx == 0
        # Single chunk: b64 fragment is complete, can decode directly
        import base64
        assert base64.b64decode(b64_frag) == data

    def test_round_trip_multi_chunk(self):
        data = bytes(range(256)) * 2  # 512 bytes
        messages = self.encoder.encode_packet(data)
        reassembly = ReassemblyBuffer()
        result = None
        for msg in messages:
            parsed = ChunkEncoder.parse_chunk(msg)
            assert parsed is not None
            msg_id, idx, total, payload = parsed
            result = reassembly.add_chunk("sender1", msg_id, idx, total, payload)
        assert result == data

    def test_message_counter_wrapping(self):
        encoder = ChunkEncoder()
        encoder._counter = 255
        msg1 = encoder.encode_packet(b"a")
        msg2 = encoder.encode_packet(b"b")
        parsed1 = ChunkEncoder.parse_chunk(msg1[0])
        parsed2 = ChunkEncoder.parse_chunk(msg2[0])
        assert parsed1[0] == 255
        assert parsed2[0] == 0  # wrapped

    def test_oversized_packet_rejected(self):
        # Create data large enough to exceed MAX_CHUNKS
        max_payload_chars = DEFAULT_MAX_MSG_LEN - HEADER_OVERHEAD
        # Each chunk carries max_payload_chars of base64 = ~max_payload_chars*3/4 bytes
        bytes_per_chunk = (max_payload_chars * 3) // 4
        data = b"\x00" * (bytes_per_chunk * MAX_CHUNKS + 100)
        with pytest.raises(ValueError, match="too large"):
            self.encoder.encode_packet(data)

    def test_is_rns_message(self):
        assert ChunkEncoder.is_rns_message("RNS|00|0|1|dGVzdA==")
        assert not ChunkEncoder.is_rns_message("Hello from MeshCore")
        assert not ChunkEncoder.is_rns_message("")

    def test_parse_non_rns_message(self):
        assert ChunkEncoder.parse_chunk("Hello world") is None
        assert ChunkEncoder.parse_chunk("") is None

    def test_parse_malformed_header(self):
        assert ChunkEncoder.parse_chunk("RNS|bad") is None
        assert ChunkEncoder.parse_chunk("RNS|xx|0|1|invalid_base64!!!") is None

    def test_message_length_within_limit(self):
        # 500-byte packet should produce messages within max_msg_len
        data = bytes(range(256)) + bytes(range(244))
        messages = self.encoder.encode_packet(data)
        for msg in messages:
            assert len(msg) <= DEFAULT_MAX_MSG_LEN


class TestReassemblyBuffer:
    def test_single_chunk_reassembly(self):
        import base64
        buf = ReassemblyBuffer()
        b64 = base64.b64encode(b"hello").decode("ascii")
        result = buf.add_chunk("sender", 0, 0, 1, b64)
        assert result == b"hello"

    def test_out_of_order_chunks(self):
        # Use the encoder to get proper b64 fragments
        encoder = ChunkEncoder(max_msg_len=30)  # small to force 2 chunks
        data = b"hello world!!"
        msgs = encoder.encode_packet(data)
        assert len(msgs) == 2
        buf = ReassemblyBuffer()
        p1 = ChunkEncoder.parse_chunk(msgs[1])
        p0 = ChunkEncoder.parse_chunk(msgs[0])
        # Send chunk 1 before chunk 0
        result = buf.add_chunk("sender", p1[0], p1[1], p1[2], p1[3])
        assert result is None
        result = buf.add_chunk("sender", p0[0], p0[1], p0[2], p0[3])
        assert result == data

    def test_multiple_senders(self):
        import base64
        buf = ReassemblyBuffer()
        a_b64 = base64.b64encode(b"alice_data").decode("ascii")
        b_b64 = base64.b64encode(b"bob_data").decode("ascii")
        r1 = buf.add_chunk("alice", 0, 0, 1, a_b64)
        r2 = buf.add_chunk("bob", 0, 0, 1, b_b64)
        assert r1 == b"alice_data"
        assert r2 == b"bob_data"

    def test_expiry_cleanup(self):
        buf = ReassemblyBuffer(expiry_seconds=0.1)
        buf.add_chunk("sender", 0, 0, 2, "cGFydGlhbA==")
        time.sleep(0.15)
        removed = buf.cleanup_expired()
        assert removed == 1
        assert len(buf._buffers) == 0

    def test_non_expired_not_cleaned(self):
        buf = ReassemblyBuffer(expiry_seconds=10)
        buf.add_chunk("sender", 0, 0, 2, "cGFydGlhbA==")
        removed = buf.cleanup_expired()
        assert removed == 0
        assert len(buf._buffers) == 1
