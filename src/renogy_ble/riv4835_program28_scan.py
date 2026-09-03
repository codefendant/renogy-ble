"""One-shot guarded RIV4835CSH1S Program 28 10 A -> 0 A validation.

Hardware discovery on the target inverter established this readback correlation
for holding register 0xE205:

    Program 28 =  0 A -> raw   0
    Program 28 =  5 A -> raw  50
    Program 28 = 10 A -> raw 100

A previous guarded hardware test successfully wrote raw 100 to 0xE205 from a
fresh pre-read of raw 50; the inverter returned a CRC-valid exact function-0x06
echo, readback became 100, and the physical LCD changed from 5 A to 10 A.

This follow-up diagnostic performs exactly one guarded Modbus function 0x06
write, and only when all preconditions are met:

* exact model hint RIV4835CSH1S
* exact target BLE address F0:F8:F2:57:47:0D
* inverter Modbus device ID 0x20
* fresh function-0x03 readback of 0xE205 equals 100 (physical Program 28 = 10 A)
* a dedicated persistent one-shot sentinel can be created before the write

If those guards pass, it writes raw 0 to 0xE205 (0.0 A). The library's normal
write_single_register path requires the CRC-valid function-0x06 response to echo
the exact device ID, register, and value. The diagnostic then reads 0xE205 back
with function 0x03 and samples line-charging current at 0x113C.

The write is never retried automatically, even across Home Assistant restarts,
and no rollback write is attempted.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from renogy_ble.ble import (
    INVERTER_COMMAND_TIMEOUT,
    INVERTER_DEVICE_ID,
    INVERTER_INIT_CHAR_UUID,
    INVERTER_INIT_DELAY,
    RIV4835CSH1S_MODEL,
    RenogyBLEDevice,
    RenogyBleClient,
    RenogyBleReadResult,
)

logger = logging.getLogger(__name__)

TARGET_ADDRESS = "F0:F8:F2:57:47:0D"
PROGRAM28_REGISTER = 0xE205
EXPECTED_PRE_RAW = 100
TARGET_RAW = 0
LINE_CHARGING_CURRENT_REGISTER = 0x113C
POST_WRITE_SETTLE_SECONDS = 1.5
SENTINEL_PATH = Path("/config/.renogy_program28_e205_10a_to_0a_attempted")

_PREFIX = "RIV4835 PROGRAM28 GUARDED WRITE TEST 10A-TO-0A"
_PATCH_MARKER = "_riv4835_program28_scan_installed"
_ATTEMPTED_ATTR = "_riv4835_program28_guarded_write_10a_to_0a_attempted"


async def _read_one_register(
    client: RenogyBleClient,
    device: RenogyBLEDevice,
    register: int,
) -> int:
    """Read one holding register with function 0x03 and validated CRC framing."""
    session = await client._prepare_session(device)
    async with session.lock:
        try:
            await client._ensure_session_ready(device, session)
            if session.client is None:
                raise RuntimeError("BLE session is not connected")

            await asyncio.sleep(INVERTER_INIT_DELAY)
            try:
                await session.client.read_gatt_char(INVERTER_INIT_CHAR_UUID)
            except Exception as exc:  # noqa: BLE001 - same best-effort init as polling
                logger.debug("%s init read failed: %s", _PREFIX, exc)

            response = await client._read_modbus_register(
                session,
                device_id=INVERTER_DEVICE_ID,
                function_code=0x03,
                register=register,
                word_count=1,
                cmd_name=f"guarded diagnostic register 0x{register:04X}",
                device_name=device.name,
                timeout=INVERTER_COMMAND_TIMEOUT,
                retries=1,
            )
            if response is None or len(response) < 7:
                raise RuntimeError(f"No valid read response for 0x{register:04X}")

            return int.from_bytes(response[3:5], "big")
        finally:
            await client._close_session(
                device.address,
                device.name,
                session,
                remove=False,
            )


async def _read_optional_line_current(
    client: RenogyBleClient,
    device: RenogyBLEDevice,
    *,
    phase: str,
) -> int | None:
    """Sample line-charging current raw value without affecting write guards."""
    try:
        return await _read_one_register(
            client,
            device,
            LINE_CHARGING_CURRENT_REGISTER,
        )
    except Exception as exc:  # noqa: BLE001 - informational telemetry only
        logger.warning("%s %s line-current read unavailable: %s", _PREFIX, phase, exc)
        return None


def _claim_persistent_one_shot() -> bool:
    """Atomically claim this exact hardware write test across HA restarts."""
    if SENTINEL_PATH.exists():
        return False

    try:
        with SENTINEL_PATH.open("x", encoding="utf-8") as sentinel:
            sentinel.write(
                "RIV4835CSH1S Program 28 guarded 10A-to-0A test\n"
                "register=0xE205\n"
                "pre_raw=100\n"
                "target_raw=0\n"
            )
    except FileExistsError:
        return False
    except OSError as exc:
        logger.error(
            "%s ABORT could not create persistent one-shot sentinel %s: %s. "
            "NO WRITE SENT.",
            _PREFIX,
            SENTINEL_PATH,
            exc,
        )
        return False

    return True


async def _run_guarded_write_test(
    client: RenogyBleClient,
    device: RenogyBLEDevice,
) -> None:
    """Attempt one guarded 10 A -> 0 A Program 28 write and never retry it."""
    attempted: set[str] = getattr(client, _ATTEMPTED_ATTR, set())
    if device.address in attempted:
        return

    # Prevent repeated attempts during this process even before persistent claim.
    attempted.add(device.address)
    setattr(client, _ATTEMPTED_ATTR, attempted)

    if device.model_hint != RIV4835CSH1S_MODEL:
        logger.error(
            "%s ABORT model guard failed: got=%s expected=%s. NO WRITE SENT.",
            _PREFIX,
            device.model_hint,
            RIV4835CSH1S_MODEL,
        )
        return

    if device.address.upper() != TARGET_ADDRESS:
        logger.error(
            "%s ABORT address guard failed: got=%s expected=%s. NO WRITE SENT.",
            _PREFIX,
            device.address,
            TARGET_ADDRESS,
        )
        return

    if client._device_id != INVERTER_DEVICE_ID:
        logger.error(
            "%s ABORT Modbus-ID guard failed: got=0x%02X expected=0x%02X. "
            "NO WRITE SENT.",
            _PREFIX,
            client._device_id,
            INVERTER_DEVICE_ID,
        )
        return

    if SENTINEL_PATH.exists():
        logger.warning(
            "%s SKIP persistent one-shot sentinel already exists at %s. "
            "NO WRITE SENT.",
            _PREFIX,
            SENTINEL_PATH,
        )
        return

    logger.warning(
        "%s BEGIN model=%s address=%s register=0x%04X expected_pre_raw=%s "
        "target_raw=%s",
        _PREFIX,
        device.model_hint,
        device.address,
        PROGRAM28_REGISTER,
        EXPECTED_PRE_RAW,
        TARGET_RAW,
    )

    try:
        pre_raw = await _read_one_register(client, device, PROGRAM28_REGISTER)
    except Exception as exc:  # noqa: BLE001 - fail closed
        logger.exception(
            "%s ABORT pre-read failed: %s. NO WRITE SENT.",
            _PREFIX,
            exc,
        )
        return

    line_before = await _read_optional_line_current(
        client,
        device,
        phase="pre-write",
    )

    logger.warning(
        "%s PRECHECK register=0x%04X raw=%s expected=%s line_charge_raw=%s",
        _PREFIX,
        PROGRAM28_REGISTER,
        pre_raw,
        EXPECTED_PRE_RAW,
        line_before if line_before is not None else "unavailable",
    )

    if pre_raw != EXPECTED_PRE_RAW:
        logger.error(
            "%s ABORT precondition failed: 0x%04X=%s expected=%s. NO WRITE SENT.",
            _PREFIX,
            PROGRAM28_REGISTER,
            pre_raw,
            EXPECTED_PRE_RAW,
        )
        return

    # Claim persistently before sending function 0x06. If HA crashes or the BLE
    # response is lost, a restart still cannot resend this write.
    if not _claim_persistent_one_shot():
        logger.error(
            "%s ABORT persistent one-shot claim failed/already exists. NO WRITE SENT.",
            _PREFIX,
        )
        return

    logger.warning(
        "%s WRITE-SEND function=0x06 device_id=0x%02X register=0x%04X raw=%s "
        "engineering=0.0A sentinel=%s",
        _PREFIX,
        INVERTER_DEVICE_ID,
        PROGRAM28_REGISTER,
        TARGET_RAW,
        SENTINEL_PATH,
    )

    # Exactly one function-0x06 send. write_single_register validates a CRC-correct
    # exact echo of device ID, register, and raw value; it does not retry writes.
    write_result = await client.write_single_register(
        device,
        PROGRAM28_REGISTER,
        TARGET_RAW,
        function_code=0x06,
    )
    if not write_result.success:
        logger.error(
            "%s RESULT FAIL write rejected/timeout: %s. No retry and no rollback "
            "write will be sent.",
            _PREFIX,
            write_result.error,
        )
        return

    logger.warning(
        "%s WRITE-ECHO PASS register=0x%04X raw=%s exact_crc_valid_echo=true",
        _PREFIX,
        PROGRAM28_REGISTER,
        TARGET_RAW,
    )

    await asyncio.sleep(POST_WRITE_SETTLE_SECONDS)

    try:
        readback_raw = await _read_one_register(client, device, PROGRAM28_REGISTER)
    except Exception as exc:  # noqa: BLE001 - do not retry or roll back
        logger.exception(
            "%s RESULT FAIL post-write readback failed: %s. No retry and no "
            "rollback write will be sent; verify LCD manually.",
            _PREFIX,
            exc,
        )
        return

    line_after = await _read_optional_line_current(
        client,
        device,
        phase="post-write",
    )

    if readback_raw != TARGET_RAW:
        logger.error(
            "%s RESULT FAIL readback 0x%04X=%s expected=%s. No retry and no "
            "rollback write will be sent; verify LCD manually.",
            _PREFIX,
            PROGRAM28_REGISTER,
            readback_raw,
            TARGET_RAW,
        )
        return

    logger.warning(
        "%s RESULT PASS pre_raw=%s write_raw=%s readback_raw=%s "
        "line_charge_before_raw=%s line_charge_after_raw=%s. "
        "Verify physical LCD Program 28 now shows 0 A and confirm AC passthrough "
        "and solar charging remain operational.",
        _PREFIX,
        pre_raw,
        TARGET_RAW,
        readback_raw,
        line_before if line_before is not None else "unavailable",
        line_after if line_after is not None else "unavailable",
    )
    logger.warning("%s END one_shot_complete=true", _PREFIX)


def install_riv4835_program28_scan() -> None:
    """Install the one-shot guarded 10 A -> 0 A test around inverter reads."""
    if getattr(RenogyBleClient, _PATCH_MARKER, False):
        return

    original = RenogyBleClient._read_inverter_device

    async def _read_inverter_device(
        self: RenogyBleClient,
        device: RenogyBLEDevice,
    ) -> RenogyBleReadResult:
        result = await original(self, device)
        if device.model_hint == RIV4835CSH1S_MODEL:
            try:
                await _run_guarded_write_test(self, device)
            except Exception:  # noqa: BLE001 - never break normal telemetry polling
                logger.exception(
                    "%s unexpected wrapper failure for %s; no automatic retry",
                    _PREFIX,
                    device.address,
                )
        return result

    RenogyBleClient._read_inverter_device = _read_inverter_device
    setattr(RenogyBleClient, _PATCH_MARKER, True)
