from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from app import create_app
from app.auth import hash_password
from app.services.einvoice_export import (
    EInvoiceExportError,
    InvoiceLine,
    OutgoingInvoice,
    Party,
    build_cii,
    build_ubl,
)
from app.services.einvoice_import import parse_einvoice
from domain.models import User


def _sample_invoice() -> OutgoingInvoice:
    return OutgoingInvoice(
        invoice_number="AR-2026-0001",
        issue_date=date(2026, 7, 6),
        seller=Party(
            name="Demo GmbH",
            street="Hauptstr. 1",
            postal_code="10115",
            city="Berlin",
            vat_id="DE123456789",
        ),
        buyer=Party(name="Kunde AG", city="München"),
        lines=[
            InvoiceLine("Beratung", Decimal("10"), Decimal("100.00"), Decimal("19.00")),
            InvoiceLine("Material", Decimal("1"), Decimal("50.00"), Decimal("7.00")),
        ],
    )


def test_totals_and_tax_groups():
    invoice = _sample_invoice()
    assert invoice.net_total == Decimal("1050.00")
    # 1000 * 19% + 50 * 7% = 190 + 3.50
    assert invoice.tax_total == Decimal("193.50")
    assert invoice.grand_total == Decimal("1243.50")
    groups = invoice.tax_groups
    assert [(g.rate, g.basis, g.tax_amount) for g in groups] == [
        (Decimal("7.00"), Decimal("50.00"), Decimal("3.50")),
        (Decimal("19.00"), Decimal("1000.00"), Decimal("190.00")),
    ]


@pytest.mark.parametrize("builder,expected_syntax", [(build_ubl, "UBL"), (build_cii, "CII")])
def test_export_roundtrips_through_parser(builder, expected_syntax):
    invoice = _sample_invoice()
    xml = builder(invoice)
    parsed = parse_einvoice(xml.encode("utf-8"))

    assert parsed.syntax == expected_syntax
    assert parsed.invoice_number == "AR-2026-0001"
    assert parsed.issue_date == date(2026, 7, 6)
    assert parsed.seller_name == "Demo GmbH"
    assert parsed.net_total == Decimal("1050.00")
    assert parsed.tax_total == Decimal("193.50")
    assert parsed.grand_total == Decimal("1243.50")
    assert parsed.primary_tax_rate == Decimal("19.00")


def test_export_requires_lines():
    with pytest.raises(EInvoiceExportError, match="mindestens eine Position"):
        OutgoingInvoice(
            invoice_number="X",
            issue_date=date(2026, 7, 6),
            seller=Party(name="A"),
            buyer=Party(name="B"),
            lines=[],
        )


def _ui_app(tmp_path: Path):
    app = create_app(
        {
            "TESTING": True,
            "DATABASE_URL": f"sqlite+pysqlite:///{tmp_path / 'test_export.db'}",
            "SELLER_VAT_ID": "DE999999999",
        }
    )
    with app.extensions["db_session_factory"]() as session:
        session.add(
            User(
                username="admin",
                password_hash=hash_password("admin123"),
                role="Admin",
                tenant_id=None,
            )
        )
        session.commit()
    return app


def test_export_endpoint_downloads_valid_xrechnung(tmp_path):
    app = _ui_app(tmp_path)
    client = app.test_client()
    client.post("/auth/login", data={"username": "admin", "password": "admin123"})
    client.post("/tenants", data={"tenant_name": "M", "company_name": "M GmbH"})

    response = client.post(
        "/erechnung/export",
        data={
            "company_id": "1",
            "syntax": "ubl",
            "invoice_number": "AR-2026-0007",
            "issue_date": "2026-07-06",
            "buyer_name": "Kunde AG",
            "buyer_city": "Köln",
            "line_name": ["Beratung", ""],
            "line_quantity": ["2", ""],
            "line_unit_price": ["150.00", ""],
            "line_tax_rate": ["19", ""],
        },
    )
    assert response.status_code == 200
    assert "application/xml" in response.headers["Content-Type"]
    assert "XRechnung_ubl_AR-2026-0007.xml" in response.headers["Content-Disposition"]

    parsed = parse_einvoice(response.get_data())
    assert parsed.invoice_number == "AR-2026-0007"
    assert parsed.seller_name == "M GmbH"
    assert parsed.net_total == Decimal("300.00")
    assert parsed.grand_total == Decimal("357.00")


def test_export_endpoint_rejects_missing_fields(tmp_path):
    app = _ui_app(tmp_path)
    client = app.test_client()
    client.post("/auth/login", data={"username": "admin", "password": "admin123"})
    client.post("/tenants", data={"tenant_name": "M", "company_name": "M GmbH"})

    response = client.post(
        "/erechnung/export",
        data={
            "company_id": "1",
            "syntax": "ubl",
            "invoice_number": "",
            "issue_date": "",
            "buyer_name": "",
        },
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert b"Pflichtfelder" in response.data
