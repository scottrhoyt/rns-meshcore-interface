# RNS-MeshCore Interface

A Reticulum network interface that uses MeshCore as the underlying transport. Two instances of this interface (on separate machines with MeshCore radios) form a "bridge" — Reticulum traffic is chunked, base64-encoded, and sent as MeshCore direct messages to the peer. The receiving side reassembles and injects packets into Reticulum.

## Installation

```bash
pip install -e ".[dev]"
```

### Dependencies

- [Reticulum](https://github.com/markqvist/Reticulum) (`rns`)
- [meshcore_py](https://github.com/fdlamotte/meshcore_py) (`meshcore`)

## Setup

### 1. Install the interface shim

Copy the interface loader to Reticulum's external interfaces directory:

```bash
mkdir -p ~/.reticulum/interfaces
cp MeshCoreInterface.py ~/.reticulum/interfaces/
```

### 2. Configure Reticulum

Add an interface section to `~/.reticulum/config`:

```ini
[[MeshCore Bridge]]
  type = MeshCoreInterface
  enabled = true
  mode = gateway

  # Connection: serial or tcp
  connection_type = serial
  serial_port = /dev/ttyACM0
  serial_baudrate = 115200

  # Peer MeshCore node public key (hex, min 12 chars)
  peer_address = a1b2c3d4e5f6

  # Airtime control
  tx_delay_ms = 500
  max_airtime_percent = 10
```

### 3. Start Reticulum

```bash
rnsd
```

## Configuration Reference

| Parameter | Default | Description |
|-----------|---------|-------------|
| `connection_type` | `serial` | `serial` or `tcp` |
| `serial_port` | — | Serial port path (e.g., `/dev/ttyACM0`) |
| `serial_baudrate` | `115200` | Serial baud rate |
| `tcp_host` | — | TCP host for network connection |
| `tcp_port` | `5555` | TCP port |
| `peer_address` | — | **Required.** Hex public key prefix of peer node (min 12 chars) |
| `tx_delay_ms` | `500` | Minimum milliseconds between transmissions |
| `max_airtime_percent` | `0` | Maximum TX duty cycle % (0 = unlimited) |
| `max_msg_len` | `140` | Max characters per MeshCore message |
| `mode` | `gateway` | Interface mode: `full`, `gateway`, `access_point`, `roaming`, `boundary`, `point_to_point` |
| `path` | *(none)* | Manual path as comma-separated hex hashes of repeater nodes (e.g. `23,5f,3a`) |
| `allow_flood_fallback` | `true` | Allow flood routing fallback when the configured path fails. When `false`, also prevents MeshCore from resetting the direct path on send failure. |
| `max_retries` | `3` | Max direct path send attempts per message |
| `max_flood_retries` | `2` | Max flood routing attempts per message (ignored if `allow_flood_fallback` is `false`) |
| `advert_on_start` | `true` | Send a flood advertisement on interface startup |
| `advert_interval` | `0` | Periodic flood advertisement interval in seconds (0 = disabled) |

## Protocol

Each MeshCore message carrying RNS data uses this format:

```
RNS|<msg_id:2hex>|<chunk_idx:1hex>|<total:1hex>|<base64_payload>
```

- Header overhead: 11 characters (`RNS|XX|X|X|`)
- ~129 characters available for base64 payload per message (at 140 char limit)
- ~96 bytes of raw binary per chunk
- Max RNS packet (500 bytes) = ~7 chunks

### Design Decisions

- **Per-chunk ACK with retry**: Each chunk uses MeshCore's `send_msg_with_retry()`, which waits for ACK and retries on failure. By default it retries up to 3 times on the direct path, then resets the path and falls back to flood routing for up to 2 more attempts. Retry counts are configurable via `max_retries` and `max_flood_retries`. Setting `allow_flood_fallback = false` disables both the flood fallback and the path reset, preserving any manually configured path.
- **URL-safe base64 encoding**: MeshCore's messaging sends text, so binary data is URL-safe base64-encoded (avoids `+`/`/` mangling). Trailing `=` padding is restored at reassembly since MeshCore strips it. This can be optimized to raw binary when `SEND_RAW_DATA` is fully implemented in meshcore_py.
- **Async bridge**: Reticulum interfaces are synchronous/threaded, while meshcore_py is fully async. An asyncio event loop runs in a dedicated daemon thread.

## Limitations

- Maximum packet size is 500 bytes (Reticulum default MTU)
- Effective throughput is limited by LoRa airtime and TX delays
- Base64 encoding adds ~37% overhead
- MeshCore encrypts direct messages at the firmware level using contact public keys, in addition to Reticulum's own encryption

## Development

```bash
# Install dev dependencies
pip install -e ".[dev]"

# Run tests
python -m pytest tests/ -v

# Run with coverage
python -m pytest tests/ --cov=rns_meshcore_interface --cov-report=term-missing
```

## Architecture

```
┌─────────────────────────────────────────────┐
│              Reticulum Transport             │
│                                             │
│  ┌─────────────────────────────────────┐    │
│  │        MeshCoreInterface            │    │
│  │  (subclass of RNS Interface)        │    │
│  │                                     │    │
│  │  ┌──────────┐  ┌────────────────┐   │    │
│  │  │ Chunking  │  │ AirtimeControl │   │    │
│  │  │ Encoder + │  │ TX delay +     │   │    │
│  │  │ Reassembly│  │ duty cycle     │   │    │
│  │  └──────────┘  └────────────────┘   │    │
│  │                                     │    │
│  │  ┌──────────────────────────────┐   │    │
│  │  │    MeshCoreTransport         │   │    │
│  │  │  (async loop in thread)      │   │    │
│  │  └──────────────────────────────┘   │    │
│  └─────────────────────────────────────┘    │
│                    │                         │
│            ┌───────┴───────┐                 │
│            │  meshcore_py  │                 │
│            │  (async API)  │                 │
│            └───────┬───────┘                 │
│                    │                         │
│         ┌──────────┴──────────┐              │
│         │  MeshCore Radio     │              │
│         │  (Serial/TCP/BLE)   │              │
│         └─────────────────────┘              │
└─────────────────────────────────────────────┘
```
