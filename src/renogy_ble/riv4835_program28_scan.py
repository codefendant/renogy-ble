"""Read-only RIV4835CSH1S register scan for locating Program 28.

This temporary hardware diagnostic uses Modbus function 0x03 only. It never
issues a Modbus write. The scanner captures raw holding-register values so two
snapshots taken with Program 28 changed manually on the inverter LCD can be
compared safely.
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

# Remaining RIV-family address space not yet compared on this exact inverter.
# Previous 0 A / 10 A snapshots ruled out readable Program-28 storage in:
#   0x0FA0-0x0FAC, 0x1004-0x1018, 0x10CB-0x10E6, 0x1129-0x1195.
# 0x10E7-0x10ED completes the known charging block; 0x1196-0x1333 completes
# the larger RIV-family range found by independent hardware reverse engineering.
SCAN_RANGES: tuple[tuple[int, int], ...] = (
    (0x10E7, 0x10ED),
    (0x1196, 0x1333),
)
SCAN_TIMEOUT = 1.5
SCAN_INTER_REQUEST_DELAY = 0.05
LOG_VALUES_PER_LINE = 12

_PATCH_MARKER = "_riv4835_program28_scan_installed"
_SCAN_DONE_ATTR = "_riv4835_program28_scan_done"


def _extract_scan_frame(
    notification_data: bytes | bytearray,
) -> tuple[str, int] | None:
    """Return a validated one-word read value or Modbus exception code.

    A normal function-03 single-register response is seven bytes:
      slave, 0x03, 0x02, value_hi, value_lo, crc_lo, crc_hi

    A Modbus exception response is five bytes:
      slave, 0x83, exception_code, crc_lo, crc_hi

    The production read helper intentionally waits only for a normal response;
    that is correct for normal telemetry but makes unsupported addresses look
    like timeouts. This diagnostic recognizes exception frames explicitly so a
    large read-only sweep can proceed quickly without weakening production I/O.
    """
    data = bytes(notification_data)

    # Search from the end so a current response wins if stale bytes ever exist.
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


async def _wait_for_scan_frame(
    session,
    *,
    timeout: float,
) -> tuple[str, int]:
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
    """Connect and perform the same best-effort init read as normal RIV polling."""
    await client._ensure_session_ready(device, session)
    if session.client is None:
        raise RuntimeError("BLE session is not connected")

    await asyncio.sleep(INVERTER_INIT_DELAY)
    try:
        await session.client.read_gatt_char(INVERTER_INIT_CHAR_UUID)
    except Exception as exc:  # noqa: BLE001 - diagnostic best effort
        logger.debug(
            "RIV4835 candidate scan init read failed for %s: %s",
            device.name,
            exc,
        )


async def _scan_one_register(
    client: RenogyBleClient,
    device: RenogyBLEDevice,
    session,
    register: int,
) -> tuple[str, int]:
    """Read exactly one register using Modbus function 0x03 only."""
    if session.client is None or not session.client.is_connected:
        await _initialize_inverter_session(client, device, session)

    client._reset_notifications(session)
    request = create_modbus_read_request(INVERTER_DEVICE_ID, 0x03, register, 1)
    await session.client.write_gatt_char(client._write_char_uuid, request)
    return await _wait_for_scan_frame(session, timeout=SCAN_TIMEOUT)


def _log_snapshot_values(snapshot: dict[int, int]) -> None:
    """Log raw values in compact, grep-friendly batches."""
    items = sorted(snapshot.items())
    for start in range(0, len(items), LOG_VALUES_PER_LINE):
        batch = items[start : start + LOG_VALUES_PER_LINE]
        logger.warning(
            "RIV4835 PROGRAM28 READ-ONLY SNAPSHOT %s",
            " ".join(f"0x{register:04X}={value}" for register, value in batch),
        )


async def _scan_program28_candidates(
    client: RenogyBleClient, device: RenogyBLEDevice
) -> None:
    """Capture one exception-aware raw register snapshot."""
    done: set[str] = getattr(client, _SCAN_DONE_ATTR, set())
    if device.address in done:
        return
    done.add(device.address)
    setattr(client, _SCAN_DONE_ATTR, done)

    snapshot: dict[int, int] = {}
    illegal_addresses: list[int] = []
    other_exceptions: dict[int, int] = {}
    timeouts: list[int] = []
    ranges_text = ",".join(
        f"0x{start:04X}-0x{end:04X}" for start, end in SCAN_RANGES
    )

    logger.warning(
        "RIV4835 PROGRAM28 READ-ONLY SNAPSHOT BEGIN device=%s ranges=%s",
        device.address,
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
                        # A late response cannot be associated safely with the
                        # next request. Reconnect before continuing.
                        await client._close_session(
                            device.address,
                            device.name,
                            session,
                            remove=False,
                        )
                    except Exception as exc:  # noqa: BLE001 - non-fatal diagnostic
                        logger.warning(
                            "RIV4835 PROGRAM28 READ-ONLY SNAPSHOT register "
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
                            snapshot[register] = response_value
                        elif response_value == 2:
                            # Illegal Data Address: expected during discovery.
                            illegal_addresses.append(register)
                        else:
                            other_exceptions[register] = response_value

                    await asyncio.sleep(SCAN_INTER_REQUEST_DELAY)
        finally:
            if client._transport_mode != "persistent_session":
                await client._close_session(
                    device.address,
                    device.name,
                    session,
                    remove=False,
                )

    _log_snapshot_values(snapshot)

    if other_exceptions:
        logger.warning(
            "RIV4835 PROGRAM28 READ-ONLY SNAPSHOT exceptions=%s",
            ",".join(
                f"0x{register:04X}:code{code}"
                for register, code in sorted(other_exceptions.items())
            ),
        )

    logger.warning(
        "RIV4835 PROGRAM28 READ-ONLY SNAPSHOT END device=%s registers=%s "
        "illegal=%s timeouts=%s",
        device.address,
        len(snapshot),
        len(illegal_addresses),
        ",".join(f"0x{register:04X}" for register in timeouts)
        if timeouts
        else "none",
    )


def install_riv4835_program28_scan() -> None:
    """Install the one-shot read-only candidate scan around inverter reads."""
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
                    "RIV4835 Program 28 read-only candidate scan failed for %s",
                    device.address,
                )
        return result

    RenogyBleClient._read_inverter_device = _read_inverter_device
    setattr(RenogyBleClient, _PATCH_MARKER, True)
