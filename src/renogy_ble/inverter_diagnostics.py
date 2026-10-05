"""Read-only inverter diagnostics from Renogy's published V1.0 protocol.

These addresses are protocol-documented, not hardware-validated for every RIV
firmware. Callers must opt in. No write operations are performed here.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

# https://platform.renogy.com/docs/ -> Inverter Modbus Protocol V1.0.
# Blocks avoid undocumented holes. Unsupported words are returned as 0xFFFF.
READ_BLOCKS = (
    (4393, 1),
    (4398, 4),
    (4405, 1),
    (4422, 1),
    (4424, 14),
    (4439, 6),
    (4447, 1),
    (4456, 1),
    (4101, 1),
)

# LCD number: (register, divisor, optional raw-value labels).
PROGRAMS: dict[int, tuple[int, float, dict[int, str] | None]] = {
    1: (4441, 1, {0: "SOL", 1: "UTI", 2: "SBU"}),
    2: (4442, 100, None),
    3: (4443, 1, {0: "APL", 1: "UPS"}),
    4: (4437, 10, None),
    5: (4439, 10, None),
    6: (4447, 1, {0: "CSo", 1: "Cub", 2: "SnU", 3: "oSo"}),
    7: (4422, 10, None),
    8: (
        4424,
        1,
        {
            0: "USE",
            1: "SLd",
            2: "FLd",
            3: "GEL",
            4: "LF14",
            5: "LF15",
            6: "LF16",
            12: "N13",
            13: "N14",
        },
    ),
    9: (4426, 10, None),
    10: (4435, 1, None),
    11: (4427, 10, None),
    12: (4431, 10, None),
    13: (4433, 1, None),
    14: (4430, 10, None),
    15: (4432, 10, None),
    17: (4425, 10, None),
    18: (4434, 1, None),
    19: (4440, 1, None),
    20: (4436, 1, None),
    22: (4444, 1, {0: "DIS", 1: "ENA", 2: "Sleep"}),
    25: (4101, 1, {0: "ENA", 1: "DIS"}),
    35: (4429, 10, None),
    37: (4428, 10, None),
    39: (4456, 10, None),
}

MACHINE_STATES = dict(
    enumerate(
        (
            "Power-on delay",
            "Wait",
            "Initialization",
            "Soft start",
            "Grid",
            "Inverter",
            "Inverter to grid",
            "Grid to inverter",
            "Hybrid",
            "Reserved",
            "Shutdown",
            "Fault",
            "Load sense",
        )
    )
)

# Keep all current fault slots, including codes absent from the LCD manual.
FAULT_NAMES = dict(
    enumerate(
        (
            "Battery low voltage protection",
            "Battery discharge software overcurrent",
            "Battery not connected",
            "Battery low voltage stop discharge",
            "Battery hardware overcurrent",
            "Charging overvoltage",
            "Bus hardware overvoltage",
            "Bus software overvoltage",
            "PV overvoltage",
            "Buck software overcurrent",
            "Buck hardware overcurrent",
            "AC power loss",
            "Bypass overload",
            "Inverter overload",
            "Inverter hardware overcurrent",
            "Inverter software overcurrent",
            "Inverter short circuit",
            "AC charging hardware overcurrent",
            "Controller overtemperature",
            "Inverter overtemperature",
            "Fan failure",
            "Memory failure",
            "Model setting error",
            "Command off",
            "Bus short circuit",
            "Relay short circuit",
            "AC charging board overtemperature",
            "AC input/output reversed",
            "Bus undervoltage",
            "Battery capacity below 10%",
            "Battery capacity below 5%",
            "Battery low energy shutdown",
            "Battery overvoltage warning",
            "Battery low voltage warning",
            "Inverter overtemperature warning",
            "Inverter overload warning",
            "Fault accumulation shutdown",
        ),
        start=1,
    )
)
FAULT_NAMES.update(
    {
        40: "Grid current over limit",
        41: "Battery reversed",
        42: "AC input undervoltage",
        43: "Ground leakage steady state",
        44: "Ground leakage 30 mA",
        45: "Ground leakage 60 mA",
        46: "Ground leakage 150 mA",
        50: "Battery overvoltage",
        51: "External probe overtemperature",
        52: "Output voltage protection",
        53: "AC current protection",
        54: "L1 voltage anomaly",
        55: "L2 voltage anomaly",
        56: "Grid current anomaly",
        57: "Inverter temperature high",
        58: "Internal temperature high",
        59: "DAB temperature high",
        60: "Backup DC bias protection",
        61: "Inverter DC bias protection",
        62: "Transformer temperature high",
        63: "Frequency out of range",
        64: "Low temperature or probe abnormal",
        65: "L2N reversed",
        66: "L1N reversed",
        67: "PE not connected",
        68: "Communication board failure",
        69: "Inverter startup failure",
    }
)
WARNING_BITS = {
    15: "Running data storage abnormal",
    6: "Internal temperature alarm",
    5: "AC input undervoltage",
    4: "AC input overvoltage",
    3: "Inverter output overcurrent",
    2: "AC input overcurrent",
    1: "Battery overvoltage",
    0: "Battery undervoltage",
}


def parse_snapshot(words: dict[int, int], errors: dict[str, str]) -> dict[str, Any]:
    """Decode only fresh, supported registers and preserve unknown raw codes."""
    supported = {address: value for address, value in words.items() if value != 0xFFFF}
    metadata: dict[str, Any] = {
        "sampled_at": datetime.now(timezone.utc).isoformat(),
        "protocol": "Renogy Inverter Modbus V1.0 (2025-06-04)",
        "hardware_validated": False,
        "raw_registers": {str(k): v for k, v in words.items()},
        "read_errors": errors,
    }
    data: dict[str, Any] = {"riv_diagnostics": metadata}
    for program, (address, divisor, labels) in PROGRAMS.items():
        if address in supported:
            raw = supported[address]
            data[f"riv_program_{program:02d}"] = (
                labels.get(raw, f"Unknown ({raw})") if labels else raw / divisor
            )
    if 4405 in supported:
        data["riv_operating_state"] = MACHINE_STATES.get(
            supported[4405], f"Unknown ({supported[4405]})"
        )
    if all(address in supported for address in range(4398, 4402)):
        slots = [supported[address] for address in range(4398, 4402)]
        active = [code for code in slots if code != 0]
        data["riv_fault_count"] = len(active)
        metadata["fault_slots"] = slots
        data["riv_active_faults"] = (
            "; ".join(
                f"{code:02d}: {FAULT_NAMES.get(code, 'Unknown fault')}"
                for code in active
            )
            or "None"
        )
    if 4393 in supported:
        raw = supported[4393]
        names = [name for bit, name in WARNING_BITS.items() if raw & (1 << bit)]
        unknown = raw & ~sum(1 << bit for bit in WARNING_BITS)
        if unknown:
            names.append(f"Unknown warning bits 0x{unknown:04X}")
        data["riv_warning_mask"] = raw
        data["riv_active_warnings"] = "; ".join(names) or "None"
    return data
