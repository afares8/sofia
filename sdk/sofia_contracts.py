"""Pure, opt-in business probes for Sofia's existing /api/ingest/event.

The caller owns the sandbox journey and its domain-specific expected values.
This module performs no HTTP, SQL, browser actions or financial transactions.
Never include customer data, cart IDs, order IDs or credentials in a result.
"""
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from typing import Literal
import json
import re


@dataclass(frozen=True)
class ProbeResult:
    check_id: str
    passed: bool
    source: Literal["business_invariant", "synthetic"] = "business_invariant"

    def incident_payload(self, *, service_id: str, tenant_id: str, environment: str, release: str) -> dict | None:
        if self.passed:
            return None
        for value in (self.check_id, service_id, tenant_id, environment, release):
            if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,120}", value):
                raise ValueError("Only non-sensitive scope identifiers are allowed")
        return {"service_id": service_id, "service_name": service_id, "level": "ERROR",
                "message": f"[CONTRACT] {self.check_id}", "detail": "Contract failed; inspect isolated evidence in CI.",
                "source": self.source, "environment": environment, "release": release,
                "tags": {"tenant_id": tenant_id, "check_id": self.check_id}}


def money(value: str) -> Decimal:
    amount = Decimal(value)
    if not amount.is_finite():
        raise ValueError("Finite decimal required")
    return amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def sale_total(*, expected_total: str, observed_total: str) -> ProbeResult:
    # Expected total comes from the app's independently calculated tax/discount contract.
    return ProbeResult("sale.total", money(expected_total) == money(observed_total))


def return_limit(*, sold_quantity: int, returned_quantity: int) -> ProbeResult:
    if type(sold_quantity) is not int or type(returned_quantity) is not int:
        raise ValueError("Integer quantities required for this example contract")
    return ProbeResult("return.quantity", 0 <= returned_quantity <= sold_quantity)


def purchase_fx(*, source_total: str, applied_rate: str, observed_total: str) -> ProbeResult:
    # Example only: per-line rounding/fees require the application's own oracle.
    rate = Decimal(applied_rate)
    if not rate.is_finite() or rate <= 0:
        raise ValueError("Positive finite FX rate required")
    expected = (money(source_total)*rate).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return ProbeResult("purchase.fx", expected == money(observed_total))


def recovered_cart(*, expected_fixture: dict, observed_fixture: dict) -> ProbeResult:
    # Synthetic fixture contains product aliases and quantities, never real customer data.
    return ProbeResult("cart.recovery", json.dumps(expected_fixture, sort_keys=True) ==
                       json.dumps(observed_fixture, sort_keys=True), source="synthetic")
