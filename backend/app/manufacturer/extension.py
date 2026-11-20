"""Read-only manufacturer extension interface. OFF unless verified.

Design rules:
  * An extension can be *registered* freely but is only *enabled* when it
    carries a complete VerificationRecord AND the user opted in.
  * Even an enabled extension cannot reach the adapter with anything that the
    protocol allowlist (app/obd/allowlist.py) rejects. Today that allowlist
    admits only generic OBD read services, so no manufacturer request can be
    sent. Adding one means: identify the ECU from the car, document the
    addressing and request from an authoritative source, add recorded
    fixtures, and extend the allowlist in a reviewed change.
  * Unverified parameters are reported as "unavailable". Never as a value.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.ecus.definitions import ECU_DEFINITIONS, EcuDefinition
from app.obd.allowlist import AllowedCommand


class ExtensionUnavailableError(Exception):
    pass


@dataclass(frozen=True)
class VerificationRecord:
    ecu_identification: str  # how the ECU variant was identified on this car
    addressing_reference: str  # authoritative source for addressing
    request_sources: tuple[str, ...]  # authoritative source per request
    fixtures: tuple[str, ...]  # recorded transcripts proving the decode

    def complete(self) -> bool:
        return bool(
            self.ecu_identification and self.addressing_reference and self.request_sources and self.fixtures
        )


class ManufacturerExtension:
    """Base class. Subclasses declare parameters; they never hold raw request strings."""

    id: str = "base"
    ecu_definition_id: str = ""
    verification: VerificationRecord | None = None

    @property
    def ecu(self) -> EcuDefinition:
        return ECU_DEFINITIONS[self.ecu_definition_id]

    def allowlisted_requests(self) -> tuple[AllowedCommand, ...]:
        """Requests this extension may send. Must already pass the protocol allowlist."""
        return ()

    def parameters(self) -> list[dict[str, object]]:
        return [
            {
                "key": p.key,
                "name": p.name,
                "unit": p.unit,
                "status": "unavailable",
                "value": None,
                "reason": p.reason,
            }
            for p in self.ecu.manufacturer_parameters
        ]

    def catalog_summary(self) -> dict[str, object] | None:
        """Request-catalog availability (BMW DDE only), with no opt-in / no confirmed variant."""
        return None

    def read(self, key: str) -> None:
        raise ExtensionUnavailableError(
            f"{self.id}: parameter {key!r} is unavailable: no verified, allowlisted read request exists."
        )


class BmwDdeExtension(ManufacturerExtension):
    id = "bmw-dde"
    ecu_definition_id = "bmw-dde-unconfirmed"

    def catalog_summary(self) -> dict[str, object] | None:
        from app.manufacturer.bmw.catalog import ExecutionContext, default_catalog

        cat = default_catalog()
        return {
            "version": cat.version,
            "metrics": [
                {
                    k: a.get(k)
                    for k in ("metric", "title", "priority", "kind", "status", "reasons", "establish_by")
                }
                for a in cat.availability(ExecutionContext())
            ],
        }


class PorscheMe78Extension(ManufacturerExtension):
    id = "porsche-me7-8"
    ecu_definition_id = "bosch-me7-8-unconfirmed"


class ExtensionRegistry:
    def __init__(self) -> None:
        self._exts: dict[str, ManufacturerExtension] = {}
        self._opt_in: set[str] = set()

    def register(self, ext: ManufacturerExtension) -> None:
        for cmd in ext.allowlisted_requests():
            if not isinstance(cmd, AllowedCommand):
                raise ExtensionUnavailableError("extensions may only declare AllowedCommand requests")
        self._exts[ext.id] = ext

    def opt_in(self, ext_id: str) -> None:
        self._opt_in.add(ext_id)

    def is_enabled(self, ext_id: str) -> bool:
        ext = self._exts.get(ext_id)
        return bool(
            ext
            and ext_id in self._opt_in
            and ext.verification
            and ext.verification.complete()
            and ext.allowlisted_requests()
        )

    def for_ecu(self, ecu_definition_id: str) -> ManufacturerExtension | None:
        return next((e for e in self._exts.values() if e.ecu_definition_id == ecu_definition_id), None)

    def status(self, ecu_definition_ids: tuple[str, ...] | None = None) -> list[dict[str, object]]:
        out = []
        for ext in self._exts.values():
            if ecu_definition_ids is not None and ext.ecu_definition_id not in ecu_definition_ids:
                continue
            out.append(
                {
                    "id": ext.id,
                    "ecu": ext.ecu.name,
                    "enabled": self.is_enabled(ext.id),
                    "verified": bool(ext.verification and ext.verification.complete()),
                    "requests": [c.text for c in ext.allowlisted_requests()],
                    "access_requirements": ext.ecu.access_requirements,
                    "parameters": ext.parameters(),
                    "catalog": ext.catalog_summary(),
                }
            )
        return out


def default_registry() -> ExtensionRegistry:
    reg = ExtensionRegistry()
    reg.register(BmwDdeExtension())
    reg.register(PorscheMe78Extension())
    return reg
