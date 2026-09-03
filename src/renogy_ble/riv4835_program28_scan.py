"""Read-only RIV4835CSH1S focused holding-register snapshot for Program 28.

The exhaustive Program 28 = 0 A discovery found 1,235 readable holding
registers outside the ranges already compared. This follow-up reads only those
known-valid addresses so a Program 28 = 10 A snapshot can be captured in about
one minute and compared with the 0 A baseline.

This temporary hardware diagnostic sends Modbus function 0x03 read requests
only. It never issues Modbus function 0x06/0x10 or any other device-setting
write.
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

# Exact readable ranges discovered on this RIV4835CSH1S at Program 28 = 0 A.
# Addresses in previously compared ranges are intentionally not repeated here.
SCAN_RANGES: tuple[tuple[int, int], ...] = (
    (0x000A, 0x0049),
    (0x0100, 0x010F),
    (0x0200, 0x0229),
    (0x0438, 0x0438),
    (0x0FAD, 0x1003),
    (0x1019, 0x10CA),
    (0x10EE, 0x1128),
    (0xDF00, 0xDF0D),
    (0xDF20, 0xDF61),
    (0xE000, 0xE025),
    (0xE100, 0xE131),
    (0xE200, 0xE21B),
    (0xF000, 0xF04D),
    (0xF800, 0xFA01),
)
SCAN_TIMEOUT = 1.0
SCAN_INTER_REQUEST_DELAY = 0.005
PROGRESS_EVERY = 0x100
LOG_VALUES_PER_LINE = 12

_PREFIX = "RIV4835 PROGRAM28 FOCUSED READ-ONLY SNAPSHOT"
_PATCH_MARKER = "_riv4835_program28_scan_installed"
_SCAN_DONE_ATTR = "_riv4835_program28_scan_done"


def _extract_scan_frame(
    notification_data: bytes | bytearray,
) -> tuple[str, int] | None:
    """Return a CRC-valid one-word read value or Modbus exception code."""
    data = bytes(notification_data)

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
            "RIV4835 focused scan init read failed for %s: %s",
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
    await session.client.write_gatt_char(client._write_char_uuid, request)
    return await _wait_for_scan_frame(session, timeout=SCAN_TIMEOUT)


def _log_nonzero_values(snapshot: dict[int, int]) -> None:
    """Log all nonzero values in compact grep-friendly batches."""
    items = sorted((address, value) for address, value in snapshot.items() if value != 0)
    for start in range(0, len(items), LOG_VALUES_PER_LINE):
        batch = items[start : start + LOG_VALUES_PER_LINE]
        logger.warning(
            "%s VALUES %s",
            _PREFIX,
            " ".join(f"0x{address:04X}={value}" for address, value in batch),
        )


async def _scan_program28_candidates(
    client: RenogyBleClient, device: RenogyBLEDevice
) -> None:
    """Capture the focused known-valid-address snapshot."""
    done: set[str] = getattr(client, _SCAN_DONE_ATTR, set())
    if device.address in done:
        return
    done.add(device.address)
    setattr(client, _SCAN_DONE_ATTR, done)

    snapshot: dict[int, int] = {}
    exceptions: dict[int, int] = {}
    timeouts: list[int] = []
    scanned = 0
    total = sum(end - start + 1 for start, end in SCAN_RANGES)

    ranges_text = ",".join(
        f"0x{start:04X}" if start == end else f"0x{start:04X}-0x{end:04X}"
        for start, end in SCAN_RANGES
    )
    logger.warning(
        "%s BEGIN device=%s addresses=%s ranges=%s",
        _PREFIX,
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
                        await client._close_session(
                            device.address,
                            device.name,
                            session,
                            remove=False,
                        )
                    except Exception as exc:  # noqa: BLE001 - non-fatal diagnostic
                        logger.warning(
                            "%s register 0x%04X failed: %s",
                            _PREFIX,
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
                        else:
                            exceptions[register] = response_value

                    scanned += 1
                    if scanned % PROGRESS_EVERY == 0:
                        logger.warning(
                            "%s PROGRESS scanned=%s/%s values=%s exceptions=%s "
                            "timeouts=%s current=0x%04X",
                            _PREFIX,
                            scanned,
                            total,
                            len(snapshot),
                            len(exceptions),
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

    _log_nonzero_values(snapshot)

    if exceptions:
        logger.warning(
            "%s EXCEPTIONS %s",
            _PREFIX,
            ",".join(
                f"0x{register:04X}:code{code}"
                for register, code in sorted(exceptions.items())
            ),
        )

    logger.warning(
        "%s END device=%s scanned=%s values=%s zero_values=%s exceptions=%s "
        "timeouts=%s",
        _PREFIX,
        device.address,
        scanned,
        len(snapshot),
        sum(1 for value in snapshot.values() if value == 0),
        len(exceptions),
        ",".join(f"0x{register:04X}" for register in timeouts)
        if timeouts
        else "none",
    )


def install_riv4835_program28_scan() -> None:
    """Install the one-shot focused read-only diagnostic around RIV reads."""
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
                    "RIV4835 Program 28 focused read-only snapshot failed for %s",
                    device.address,
                )
        return result

    RenogyBleClient._read_inverter_device = _read_inverter_device
    setattr(RenogyBleClient, _PATCH_MARKER, True)
