"""Read-only RIV4835CSH1S holding-register discovery for Program 28.

This temporary hardware diagnostic sends Modbus function 0x03 read requests
only. It never issues Modbus function 0x06/0x10 or any other device-setting
write. The current pass exhaustively checks every address outside ranges already
captured at both Program 28 = 0 A and 10 A, looking for model-specific holding
registers that were absent from the published/reverse-engineered RIV-family map.
"""

from __future__ import annotations

import asyncio
import logging

from renogy_ble.ble import (
    INVERTER_DEVICE_ID,
    INVERTER_INIT_CHAR_UUID,
    INVERTER_INIT_DELAY,
    RIV4835CSH1S_MODEL,
    RenogyBLEDevice,
    RenogyBleClient,
    RenogyBleReadResult,
    create_modbus_read_request,
    modbus_crc,
)

logger = logging.getLogger(__name__)

# Already compared at Program 28 = 0 A and 10 A and therefore intentionally
# omitted here:
#   0x0FA0-0x0FAC
#   0x1004-0x1018
#   0x10CB-0x10ED
#   0x1129-0x1333
#
# Together, the ranges below are every remaining 16-bit Modbus address.
SCAN_RANGES: tuple[tuple[int, int], ...] = (
    (0x0000, 0x0F9F),
    (0x0FAD, 0x1003),
    (0x1019, 0x10CA),
    (0x10EE, 0x1128),
    (0x1334, 0xFFFF),
)
SCAN_TIMEOUT = 1.0
# The 9600-baud Modbus/BT-2 path already rate-limits each request naturally.
# A very small cooperative pause keeps the HA event loop responsive without
# materially extending this hour-scale exhaustive discovery pass.
SCAN_INTER_REQUEST_DELAY = 0.005
PROGRESS_EVERY = 0x1000
LOG_VALUES_PER_LINE = 12

_PATCH_MARKER = "_riv4835_program28_scan_installed"
_SCAN_DONE_ATTR = "_riv4835_program28_scan_done"


def _extract_scan_frame(
    notification_data: bytes | bytearray,
) -> tuple[str, int] | None:
    """Return a CRC-valid one-word read value or Modbus exception code."""
    data = bytes(notification_data)

    # Search backward so the most recent complete frame wins if multiple
    # notification fragments have accumulated.
    for offset in range(max(0, len(data) - 7), -1, -1):
        if offset + 7 <= len(data):
            candidate = data[offset : offset + 7]
            if (
                candidate[0] == INVERTER_DEVICE_ID
                and candidate[1] == 0x03
                and candidate[2] == 0x02
            ):
                crc_low, crc_high = modbus_crc(candidate[:-2])
                if candidate[-2:] == bytes([crc_low, crc_high]):
                    return ("value", int.from_bytes(candidate[3:5], "big"))

        if offset + 5 <= len(data):
            candidate = data[offset : offset + 5]
            if candidate[0] == INVERTER_DEVICE_ID and candidate[1] == 0x83:
                crc_low, crc_high = modbus_crc(candidate[:3])
                if candidate[3:5] == bytes([crc_low, crc_high]):
                    return ("exception", candidate[2])

    return None


async def _wait_for_scan_frame(session, *, timeout: float) -> tuple[str, int]:
    """Wait for a validated normal or exception Modbus response."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout

    while True:
        frame = _extract_scan_frame(session.notification_data)
        if frame is not None:
            return frame

        remaining = deadline - loop.time()
        if remaining <= 0:
            raise asyncio.TimeoutError()

        await asyncio.wait_for(session.notification_event.wait(), remaining)
        session.notification_event.clear()


async def _initialize_inverter_session(
    client: RenogyBleClient,
    device: RenogyBLEDevice,
    session,
) -> None:
    """Connect and perform the same best-effort init as normal RIV polling."""
    await client._ensure_session_ready(device, session)
    if session.client is None:
        raise RuntimeError("BLE session is not connected")

    await asyncio.sleep(INVERTER_INIT_DELAY)
    try:
        await session.client.read_gatt_char(INVERTER_INIT_CHAR_UUID)
    except Exception as exc:  # noqa: BLE001 - diagnostic best effort
        logger.debug(
            "RIV4835 exhaustive scan init read failed for %s: %s",
            device.name,
            exc,
        )


async def _scan_one_register(
    client: RenogyBleClient,
    device: RenogyBLEDevice,
    session,
    register: int,
) -> tuple[str, int]:
    """Read exactly one holding register using Modbus function 0x03."""
    if session.client is None or not session.client.is_connected:
        await _initialize_inverter_session(client, device, session)

    client._reset_notifications(session)
    request = create_modbus_read_request(INVERTER_DEVICE_ID, 0x03, register, 1)
    # BLE characteristic writes are the transport for the Modbus READ request;
    # the Modbus function in this frame is strictly 0x03.
    await session.client.write_gatt_char(client._write_char_uuid, request)
    return await _wait_for_scan_frame(session, timeout=SCAN_TIMEOUT)


def _compress_addresses(addresses: list[int]) -> list[tuple[int, int]]:
    """Compress sorted addresses into contiguous inclusive ranges."""
    if not addresses:
        return []

    sorted_addresses = sorted(set(addresses))
    ranges: list[tuple[int, int]] = []
    start = previous = sorted_addresses[0]

    for address in sorted_addresses[1:]:
        if address == previous + 1:
            previous = address
            continue
        ranges.append((start, previous))
        start = previous = address

    ranges.append((start, previous))
    return ranges


def _format_ranges(ranges: list[tuple[int, int]]) -> str:
    """Format inclusive ranges compactly for logs."""
    return ",".join(
        f"0x{start:04X}" if start == end else f"0x{start:04X}-0x{end:04X}"
        for start, end in ranges
    ) or "none"


def _log_nonzero_values(snapshot: dict[int, int]) -> None:
    """Log only nonzero discovered values in compact grep-friendly batches."""
    items = sorted((address, value) for address, value in snapshot.items() if value != 0)
    for start in range(0, len(items), LOG_VALUES_PER_LINE):
        batch = items[start : start + LOG_VALUES_PER_LINE]
        logger.warning(
            "RIV4835 PROGRAM28 FULL READ-ONLY DISCOVERY VALUES %s",
            " ".join(f"0x{address:04X}={value}" for address, value in batch),
        )


async def _scan_program28_candidates(
    client: RenogyBleClient, device: RenogyBLEDevice
) -> None:
    """Exhaustively discover previously unknown readable holding registers."""
    done: set[str] = getattr(client, _SCAN_DONE_ATTR, set())
    if device.address in done:
        return
    done.add(device.address)
    setattr(client, _SCAN_DONE_ATTR, done)

    snapshot: dict[int, int] = {}
    valid_addresses: list[int] = []
    illegal_count = 0
    other_exceptions: dict[int, int] = {}
    timeouts: list[int] = []
    scanned = 0
    total = sum(end - start + 1 for start, end in SCAN_RANGES)

    ranges_text = ",".join(
        f"0x{start:04X}-0x{end:04X}" for start, end in SCAN_RANGES
    )
    logger.warning(
        "RIV4835 PROGRAM28 FULL READ-ONLY DISCOVERY BEGIN device=%s "
        "addresses=%s ranges=%s",
        device.address,
        total,
        ranges_text,
    )

    session = await client._prepare_session(device)
    async with session.lock:
        try:
            await _initialize_inverter_session(client, device, session)

            for range_start, range_end in SCAN_RANGES:
                for register in range(range_start, range_end + 1):
                    try:
                        response_type, response_value = await _scan_one_register(
                            client, device, session, register
                        )
                    except asyncio.TimeoutError:
                        timeouts.append(register)
                        # A late response cannot safely be matched to the next
                        # address, so reconnect before continuing.
                        await client._close_session(
                            device.address,
                            device.name,
                            session,
                            remove=False,
                        )
                    except Exception as exc:  # noqa: BLE001 - non-fatal diagnostic
                        logger.warning(
                            "RIV4835 PROGRAM28 FULL READ-ONLY DISCOVERY register "
                            "0x%04X failed: %s",
                            register,
                            exc,
                        )
                        timeouts.append(register)
                        await client._close_session(
                            device.address,
                            device.name,
                            session,
                            remove=False,
                        )
                    else:
                        if response_type == "value":
                            valid_addresses.append(register)
                            snapshot[register] = response_value
                        elif response_value == 2:
                            illegal_count += 1
                        else:
                            other_exceptions[register] = response_value

                    scanned += 1
                    if scanned % PROGRESS_EVERY == 0:
                        logger.warning(
                            "RIV4835 PROGRAM28 FULL READ-ONLY DISCOVERY PROGRESS "
                            "scanned=%s/%s valid=%s illegal=%s timeouts=%s current=0x%04X",
                            scanned,
                            total,
                            len(valid_addresses),
                            illegal_count,
                            len(timeouts),
                            register,
                        )

                    if SCAN_INTER_REQUEST_DELAY:
                        await asyncio.sleep(SCAN_INTER_REQUEST_DELAY)
        finally:
            await client._close_session(
                device.address,
                device.name,
                session,
                remove=False,
            )

    valid_ranges = _compress_addresses(valid_addresses)
    logger.warning(
        "RIV4835 PROGRAM28 FULL READ-ONLY DISCOVERY VALID-RANGES %s",
        _format_ranges(valid_ranges),
    )
    _log_nonzero_values(snapshot)

    if other_exceptions:
        logger.warning(
            "RIV4835 PROGRAM28 FULL READ-ONLY DISCOVERY EXCEPTIONS %s",
            ",".join(
                f"0x{register:04X}:code{code}"
                for register, code in sorted(other_exceptions.items())
            ),
        )

    logger.warning(
        "RIV4835 PROGRAM28 FULL READ-ONLY DISCOVERY END device=%s scanned=%s "
        "valid=%s zero_valid=%s illegal=%s timeouts=%s",
        device.address,
        scanned,
        len(valid_addresses),
        sum(1 for value in snapshot.values() if value == 0),
        illegal_count,
        ",".join(f"0x{register:04X}" for register in timeouts)
        if timeouts
        else "none",
    )


def install_riv4835_program28_scan() -> None:
    """Install the one-shot exhaustive read-only diagnostic around RIV reads."""
    if getattr(RenogyBleClient, _PATCH_MARKER, False):
        return

    original = RenogyBleClient._read_inverter_device

    async def _read_inverter_device(
        self: RenogyBleClient, device: RenogyBLEDevice
    ) -> RenogyBleReadResult:
        result = await original(self, device)
        if device.model_hint == RIV4835CSH1S_MODEL:
            try:
                await _scan_program28_candidates(self, device)
            except Exception:  # noqa: BLE001 - never break normal telemetry
                logger.exception(
                    "RIV4835 Program 28 exhaustive read-only discovery failed for %s",
                    device.address,
                )
        return result

    RenogyBleClient._read_inverter_device = _read_inverter_device
    setattr(RenogyBleClient, _PATCH_MARKER, True)
