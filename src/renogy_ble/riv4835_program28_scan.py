"""Read-only RIV4835CSH1S register scan for locating Program 28.

This diagnostic intentionally uses Modbus function 0x03 only. It performs one
focused snapshot per RenogyBleClient/device instance and logs raw holding
register values for later before/after comparison while Program 28 is changed
manually on the inverter LCD.
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
)

logger = logging.getLogger(__name__)

# Candidate RIV-family blocks not yet compared on this exact RIV4835CSH1S.
# 0x1129-0x1195 has already been tested at Program 28 = 0 A and 10 A and did
# not contain a readable persistent field that tracked the setting.
SCAN_RANGES: tuple[tuple[int, int], ...] = (
    (0x0FA0, 0x0FAC),
    (0x1004, 0x1018),
    (0x10CB, 0x10E6),
)
SCAN_CHUNK_WORDS = 8
SCAN_TIMEOUT = 2.0
SCAN_INTER_REQUEST_DELAY = 0.20

_PATCH_MARKER = "_riv4835_program28_scan_installed"
_SCAN_DONE_ATTR = "_riv4835_program28_scan_done"


def _decode_registers(start_register: int, response: bytes) -> dict[int, int]:
    """Decode a validated Modbus 0x03 response into raw unsigned words."""
    byte_count = response[2]
    payload = response[3 : 3 + byte_count]
    values: dict[int, int] = {}
    for index in range(0, len(payload), 2):
        values[start_register + index // 2] = int.from_bytes(
            payload[index : index + 2], "big"
        )
    return values


async def _read_candidate_range(
    client: RenogyBleClient,
    device: RenogyBLEDevice,
    start_register: int,
    word_count: int,
) -> bytes | None:
    """Read one candidate range using function 0x03 only on an isolated session."""
    session = await client._prepare_session(device)

    async with session.lock:
        try:
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

            return await client._read_modbus_register(
                session,
                device_id=INVERTER_DEVICE_ID,
                function_code=0x03,
                register=start_register,
                word_count=word_count,
                cmd_name=(
                    "RIV4835 Program 28 read-only candidate scan "
                    f"0x{start_register:04X}"
                ),
                device_name=device.name,
                timeout=SCAN_TIMEOUT,
                retries=1,
            )
        finally:
            # Isolate every candidate request so a timeout or late response can
            # never contaminate the next address range.
            await client._close_session(
                device.address,
                device.name,
                session,
                remove=True,
            )


async def _scan_block(
    client: RenogyBleClient,
    device: RenogyBLEDevice,
    block_start: int,
    block_end: int,
    snapshot: dict[int, int],
    missing_registers: list[int],
) -> None:
    """Scan one candidate block, falling back to individual reads on failures."""
    start = block_start
    while start <= block_end:
        count = min(SCAN_CHUNK_WORDS, block_end - start + 1)
        try:
            response = await _read_candidate_range(client, device, start, count)
        except Exception as exc:  # noqa: BLE001 - non-fatal diagnostic
            logger.warning(
                "RIV4835 PROGRAM28 READ-ONLY SNAPSHOT block 0x%04X-0x%04X failed: %s",
                start,
                start + count - 1,
                exc,
            )
            response = None

        if response is not None:
            values = _decode_registers(start, response)
            snapshot.update(values)
            logger.warning(
                "RIV4835 PROGRAM28 READ-ONLY SNAPSHOT %s",
                " ".join(
                    f"0x{register:04X}={value}"
                    for register, value in sorted(values.items())
                ),
            )
        else:
            logger.warning(
                "RIV4835 PROGRAM28 READ-ONLY SNAPSHOT retrying block "
                "0x%04X-0x%04X one register at a time",
                start,
                start + count - 1,
            )
            recovered: dict[int, int] = {}
            for register in range(start, start + count):
                try:
                    single = await _read_candidate_range(client, device, register, 1)
                except Exception as exc:  # noqa: BLE001 - non-fatal diagnostic
                    logger.warning(
                        "RIV4835 PROGRAM28 READ-ONLY SNAPSHOT register "
                        "0x%04X failed: %s",
                        register,
                        exc,
                    )
                    single = None

                if single is None:
                    missing_registers.append(register)
                else:
                    recovered.update(_decode_registers(register, single))
                await asyncio.sleep(SCAN_INTER_REQUEST_DELAY)

            if recovered:
                snapshot.update(recovered)
                logger.warning(
                    "RIV4835 PROGRAM28 READ-ONLY SNAPSHOT %s",
                    " ".join(
                        f"0x{register:04X}={value}"
                        for register, value in sorted(recovered.items())
                    ),
                )

        start += count
        if start <= block_end:
            await asyncio.sleep(SCAN_INTER_REQUEST_DELAY)


async def _scan_program28_candidates(
    client: RenogyBleClient, device: RenogyBLEDevice
) -> None:
    """Capture and log one raw candidate-register snapshot."""
    done: set[str] = getattr(client, _SCAN_DONE_ATTR, set())
    if device.address in done:
        return
    done.add(device.address)
    setattr(client, _SCAN_DONE_ATTR, done)

    snapshot: dict[int, int] = {}
    missing_registers: list[int] = []
    ranges_text = ",".join(
        f"0x{start:04X}-0x{end:04X}" for start, end in SCAN_RANGES
    )

    logger.warning(
        "RIV4835 PROGRAM28 READ-ONLY SNAPSHOT BEGIN device=%s ranges=%s",
        device.address,
        ranges_text,
    )

    for block_start, block_end in SCAN_RANGES:
        await _scan_block(
            client,
            device,
            block_start,
            block_end,
            snapshot,
            missing_registers,
        )

    logger.warning(
        "RIV4835 PROGRAM28 READ-ONLY SNAPSHOT END device=%s registers=%s missing=%s",
        device.address,
        len(snapshot),
        ",".join(f"0x{register:04X}" for register in missing_registers)
        if missing_registers
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
