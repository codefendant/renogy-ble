"""Temporary RIV4835CSH1S Program 28 readback support for hardware validation."""

from __future__ import annotations

from renogy_ble.ble import (
    RIV4835CSH1S_MODEL,
    RenogyBleClient,
    _InverterReadSpec,
)

_PATCH_MARKER = "_riv4835_program28_readback_installed"


def install_riv4835_program28_readback() -> None:
    """Add register 0x1146 to the RIV4835CSH1S read profile once."""
    if getattr(RenogyBleClient, _PATCH_MARKER, False):
        return

    original = RenogyBleClient._inverter_read_specs

    def _inverter_read_specs(
        model_hint: str | None,
    ) -> tuple[_InverterReadSpec, ...]:
        specs = original(model_hint)
        if model_hint != RIV4835CSH1S_MODEL:
            return specs
        if any(spec.register == 0x1146 for spec in specs):
            return specs
        return (
            *specs,
            _InverterReadSpec(
                0x1146,
                1,
                "_parse_inverter_charge_current",
            ),
        )

    RenogyBleClient._inverter_read_specs = staticmethod(_inverter_read_specs)
    setattr(RenogyBleClient, _PATCH_MARKER, True)
