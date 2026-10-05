"""Read-only diagnostic parsing and timeout isolation."""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from renogy_ble.ble import RenogyBleClient, modbus_crc
from renogy_ble.inverter_diagnostics import READ_BLOCKS, parse_snapshot


def frame(count: int, value: int = 0) -> bytes:
    """Build a validated FC03 frame."""
    payload = bytes([0x20, 3, count * 2]) + value.to_bytes(2, "big") * count
    return payload + bytes(modbus_crc(payload))


def test_settings_and_multiple_faults() -> None:
    """Keep concurrent and unknown faults and use the documented scales."""
    data = parse_snapshot(
        {
            4437: 480,
            4439: 548,
            4447: 3,
            4398: 1,
            4399: 18,
            4400: 99,
            4401: 0,
            4393: 0x101,
            4405: 4,
        },
        {},
    )
    assert data["riv_program_04"] == 48
    assert data["riv_program_05"] == 54.8
    assert data["riv_program_06"] == "oSo"
    assert data["riv_fault_count"] == 3
    assert "18: AC charging hardware overcurrent" in data["riv_active_faults"]
    assert "99: Unknown fault" in data["riv_active_faults"]
    assert "Unknown warning bits 0x0100" in data["riv_active_warnings"]
    assert data["riv_operating_state"] == "Grid"
    assert data["riv_diagnostics"]["fault_slots"] == [1, 18, 99, 0]


def test_missing_and_unsupported_are_not_no_fault() -> None:
    """Missing slots or 0xFFFF cannot falsely report a healthy inverter."""
    data = parse_snapshot(
        {4398: 0, 4399: 0, 4400: 0, 4401: 0xFFFF, 4437: 0xFFFF}, {"4447": "timeout"}
    )
    assert "riv_fault_count" not in data
    assert "riv_active_faults" not in data
    assert "riv_program_04" not in data
    assert data["riv_diagnostics"]["raw_registers"]["4437"] == 0xFFFF


def test_zero_is_valid_and_unknown_enum_is_preserved() -> None:
    """Distinguish zero faults from no telemetry and preserve new enum values."""
    data = parse_snapshot(
        {4398: 0, 4399: 0, 4400: 0, 4401: 0, 4393: 0, 4447: 99, 4101: 0}, {}
    )
    assert data["riv_active_faults"] == "None"
    assert data["riv_active_warnings"] == "None"
    assert data["riv_fault_count"] == 0
    assert data["riv_program_06"] == "Unknown (99)"
    assert data["riv_program_25"] == "ENA"


def test_reader_is_fc03_only_and_recovers_before_next_block(monkeypatch) -> None:
    """A diagnostic timeout preserves normal data and discards its session."""
    client = RenogyBleClient(transport_mode="persistent_session")
    device = MagicMock(device_type="inverter", model_hint="RIV4835CSH1S")
    device.parsed_data = {"battery_voltage": 50.2}
    sessions = []
    reads = []
    closes = []

    async def prepare(_device):
        session = MagicMock()
        session.lock = asyncio.Lock()
        session.client.read_gatt_char = AsyncMock()
        sessions.append(session)
        return session

    async def read(session, **kwargs):
        reads.append(kwargs)
        assert kwargs["function_code"] == 3
        assert kwargs["retries"] == 1
        if len(reads) == 1:
            return None
        assert closes[0] == (sessions[0], True)
        assert session is not sessions[0]
        return frame(kwargs["word_count"])

    async def close(_address, _name, session, *, remove):
        closes.append((session, remove))

    monkeypatch.setattr(client, "_prepare_session", prepare)
    monkeypatch.setattr(client, "_ensure_session_ready", AsyncMock())
    monkeypatch.setattr(client, "_read_modbus_register", read)
    monkeypatch.setattr(client, "_close_session", close)
    monkeypatch.setattr("renogy_ble.ble.asyncio.sleep", AsyncMock())
    data = asyncio.run(client.read_inverter_diagnostics(device))
    assert len(reads) == len(READ_BLOCKS)
    assert "4393" in data["riv_diagnostics"]["read_errors"]
    assert "riv_active_warnings" not in data
    assert data["riv_active_faults"] == "None"
    assert device.parsed_data == {"battery_voltage": 50.2}


def test_reader_rejects_other_models() -> None:
    """Never use model-specific probes on a generic inverter."""
    with pytest.raises(ValueError):
        asyncio.run(
            RenogyBleClient().read_inverter_diagnostics(
                MagicMock(device_type="inverter", model_hint=None)
            )
        )
