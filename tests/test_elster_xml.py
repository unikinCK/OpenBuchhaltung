"""ElsterXML-Datensatz der UStVA, ERiC-Hilfsfunktionen und Bundesfinanzamtsnummer."""

from __future__ import annotations

import os
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from xml.etree import ElementTree as ET

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.services.company_master_data import (
    CompanyMasterDataError,
    update_company_master_data,
)
from app.services.elster_xml import (
    ELSTER_NS,
    ELSTER_TESTMERKER,
    USTVA_KENNZAHL_ORDER,
    ElsterXmlError,
    ElsterXmlOptions,
    build_ustva_elster_xml,
    elster_tax_number,
    ustva_datenart_version,
    ustva_kennzahl_values,
    ustva_period,
)
from app.services.eric import parse_findings, parse_transfer_ticket
from domain.models import Base, Company, Tenant

USTVA_NS = "http://finkonsens.de/elster/elsteranmeldung/ustva/v2026"


def _company(**overrides: object) -> SimpleNamespace:
    values = {
        "name": "Muster GmbH",
        "street": "Musterstraße 12a",
        "postal_code": "12345",
        "city": "Musterstadt",
        "phone": "0123 456789",
        "email": "info@example.org",
        "tax_number": "1096081508187",
        "tax_office_number": None,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _vat_return(period_label: str = "2026-Q3", rows: list[tuple[str, str]] | None = None):
    rows = rows or [
        ("81", "10000.55"),
        ("86", "500.40"),
        ("41", "2000.00"),
        ("USt", "1935.13"),
        ("66", "800.21"),
        ("46", "300.00"),
        ("47", "57.00"),
        ("67", "57.00"),
        ("83", "1134.92"),
    ]
    return SimpleNamespace(
        id=42,
        period_label=period_label,
        kennzahlen=[{"kennziffer": code, "label": "", "amount": amount} for code, amount in rows],
    )


def _build(**kwargs: object) -> str:
    options = kwargs.pop("options", ElsterXmlOptions(hersteller_id="12345", product_version="1.0"))
    return build_ustva_elster_xml(
        vat_return=kwargs.pop("vat_return", _vat_return()),
        company=kwargs.pop("company", _company()),
        options=options,
        created_on=date(2026, 10, 5),
    )


def test_ustva_period_maps_months_and_quarters() -> None:
    assert ustva_period("2026-05") == ustva_period("2026-05")
    assert ustva_period("2026-05").zeitraum == "05"
    assert ustva_period("2026-Q1").zeitraum == "41"
    assert ustva_period("2026-Q4").zeitraum == "44"
    assert ustva_datenart_version(2026) == "UStVA_2026"
    for label in ("2026", "2026-H1", "2026-13"):
        with pytest.raises(ElsterXmlError, match="nicht übermittelbar"):
            ustva_period(label)


def test_elster_tax_number_from_notice_format() -> None:
    # Bayern FFF/BBB/UUUUP, Hessen 0FF BBB UUUUP, NRW FFF/BBBB/UUUP: immer die letzten acht Ziffern.
    assert elster_tax_number("181/815/08155", "9181") == "9181081508155"
    assert elster_tax_number("013 815 08153", "2613") == "2613081508153"
    assert elster_tax_number("133/8150/8159", "5133") == "5133081508159"
    assert elster_tax_number("1096081508187", None) == "1096081508187"
    assert elster_tax_number("1096 0815 08187", "1096") == "1096081508187"


@pytest.mark.parametrize(
    ("tax_number", "office", "message"),
    [
        (None, "9181", "fehlt die Steuernummer"),
        ("181/815/08155", None, "Bundesfinanzamtsnummer"),
        ("181/815/08155", "918", "vierstellig"),
        ("1096081508187", "9181", "passt nicht"),
        ("1096181508187", None, "Stelle 5"),
        ("12345", "9181", "10 oder 11 Ziffern"),
    ],
)
def test_elster_tax_number_rejects_incomplete_data(
    tax_number: str | None, office: str | None, message: str
) -> None:
    with pytest.raises(ElsterXmlError, match=message):
        elster_tax_number(tax_number, office)


def test_kennzahl_values_round_and_skip_zero() -> None:
    values = ustva_kennzahl_values(
        [
            {"kennziffer": "81", "amount": "1000.99"},
            {"kennziffer": "86", "amount": "-10.99"},
            {"kennziffer": "41", "amount": "0.40"},
            {"kennziffer": "66", "amount": "12.345"},
            {"kennziffer": "61", "amount": "0.00"},
            {"kennziffer": "USt", "amount": "190.19"},
            {"kennziffer": "83", "amount": "0"},
        ]
    )
    # Bemessungsgrundlagen in vollen Euro (Cent entfallen), Steuer kaufmännisch auf Cent.
    assert values == {"81": "1000", "86": "-10", "66": "12.35", "83": "0.00"}


def test_kennzahl_values_require_83_and_known_codes() -> None:
    with pytest.raises(ElsterXmlError, match="Kennzahl 83"):
        ustva_kennzahl_values([{"kennziffer": "81", "amount": "1"}])
    with pytest.raises(ElsterXmlError, match="Kennzahl 99"):
        ustva_kennzahl_values([{"kennziffer": "99", "amount": "1"}])
    with pytest.raises(ElsterXmlError, match="gültigen Betrag"):
        ustva_kennzahl_values([{"kennziffer": "83", "amount": "abc"}])


def test_build_ustva_elster_xml_structure() -> None:
    xml = _build()
    assert xml.startswith('<?xml version="1.0" encoding="UTF-8"?>\n<Elster xmlns="')
    # ERiC lehnt Präfix-Definitionen ab: nur Standard-Namensräume.
    assert "xmlns:" not in xml
    assert f'<Anmeldungssteuern xmlns="{USTVA_NS}" version="2026">' in xml

    root = ET.fromstring(xml)
    e = f"{{{ELSTER_NS}}}"
    u = f"{{{USTVA_NS}}}"
    header = root.find(f"{e}TransferHeader")
    assert header.findtext(f"{e}Verfahren") == "ElsterAnmeldung"
    assert header.findtext(f"{e}DatenArt") == "UStVA"
    assert header.findtext(f"{e}Vorgang") == "send-Auth"
    assert header.findtext(f"{e}Testmerker") == ELSTER_TESTMERKER
    assert header.findtext(f"{e}HerstellerID") == "12345"
    block = root.find(f"{e}DatenTeil/{e}Nutzdatenblock")
    nutzdaten_header = block.find(f"{e}NutzdatenHeader")
    assert nutzdaten_header.find(f"{e}Empfaenger").attrib == {"id": "F"}
    assert nutzdaten_header.findtext(f"{e}Empfaenger") == "1096"
    assert nutzdaten_header.findtext(f"{e}Hersteller/{e}ProduktName") == "OpenBuchhaltung"

    anmeldung = block.find(f"{e}Nutzdaten/{u}Anmeldungssteuern")
    assert anmeldung.findtext(f"{u}Erstellungsdatum") == "20261005"
    unternehmer = anmeldung.find(f"{u}Steuerfall/{u}Unternehmer")
    assert unternehmer.findtext(f"{u}Str") == "Musterstraße"
    assert unternehmer.findtext(f"{u}Hausnummer") == "12"
    assert unternehmer.findtext(f"{u}HNrZusatz") == "a"
    voranmeldung = anmeldung.find(f"{u}Steuerfall/{u}Umsatzsteuervoranmeldung")
    tags = [child.tag.removeprefix(u) for child in voranmeldung]
    assert tags[:4] == ["Jahr", "Zeitraum", "Steuernummer", "Kz09"]
    kennzahlen = [tag.removeprefix("Kz") for tag in tags[4:]]
    assert kennzahlen == [code for code in USTVA_KENNZAHL_ORDER if code in kennzahlen]
    assert voranmeldung.findtext(f"{u}Zeitraum") == "43"
    assert voranmeldung.findtext(f"{u}Kz81") == "10000"
    assert voranmeldung.findtext(f"{u}Kz83") == "1134.92"
    assert voranmeldung.find(f"{u}KzUSt") is None


def test_build_ustva_elster_xml_production_has_no_testmerker() -> None:
    xml = _build(options=ElsterXmlOptions(hersteller_id="12345", test=False))
    assert "Testmerker" not in xml


def test_build_ustva_elster_xml_validates_inputs() -> None:
    with pytest.raises(ElsterXmlError, match="Hersteller-ID"):
        _build(options=ElsterXmlOptions(hersteller_id="1234"))
    with pytest.raises(ElsterXmlError, match="Prüfmodul"):
        _build(vat_return=_vat_return("2027-01"))
    with pytest.raises(ElsterXmlError, match="Bundesfinanzamtsnummer"):
        _build(company=_company(tax_number="181/815/08155"))
    xml = _build(company=_company(street="Am Markt", postal_code="D-123"))
    assert "<Hausnummer>" not in xml
    assert "<Str>Am Markt</Str>" in xml


def test_parse_findings_and_transfer_ticket() -> None:
    response = (
        '<EricBearbeiteVorgang xmlns="http://www.elster.de/EricXML/1.2/EricBearbeiteVorgang">'
        "<FehlerRegelpruefung><Feldidentifikator>/Anmeldungssteuern[1]/Steuerfall[1]/"
        "Umsatzsteuervoranmeldung[1]/Steuernummer[1]</Feldidentifikator>"
        "<Text>Bitte machen Sie Angaben im Feld '$/Elster[1]/DatenTeil[1]/"
        "Steuernummer[1]$'.</Text></FehlerRegelpruefung></EricBearbeiteVorgang>"
    )
    assert parse_findings(response) == [
        "Steuernummer: Bitte machen Sie Angaben im Feld 'Steuernummer'."
    ]
    assert parse_findings("") == []
    assert parse_findings("<kaputt") == []
    assert parse_transfer_ticket("", "<a><TransferTicket> tt-1 </TransferTicket></a>") == "tt-1"
    assert parse_transfer_ticket("<a/>") is None


@pytest.fixture()
def session() -> Session:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as test_session:
        yield test_session


def test_company_tax_office_number(session: Session) -> None:
    tenant = Tenant(name="Bufa")
    company = Company(tenant=tenant, name="Bufa GmbH", currency_code="EUR")
    session.add_all([tenant, company])
    session.flush()
    assert update_company_master_data(
        session=session, company=company, changed_by="pytest", tax_office_number=" 9181 "
    )
    assert company.tax_office_number == "9181"
    for value in ("918", "91a1", 9181):
        with pytest.raises(CompanyMasterDataError, match="Bundesfinanzamtsnummer"):
            update_company_master_data(
                session=session, company=company, changed_by="pytest", tax_office_number=value
            )
    update_company_master_data(
        session=session, company=company, changed_by="pytest", tax_office_number=""
    )
    assert company.tax_office_number is None


ERIC_LIBRARY = os.environ.get("OBH_TEST_ERIC_LIBRARY")


@pytest.mark.skipif(not ERIC_LIBRARY, reason="OBH_TEST_ERIC_LIBRARY zeigt nicht auf ERiC")
def test_generated_ustva_passes_real_eric_validation(tmp_path: Path) -> None:
    """Prüft den erzeugten Datensatz mit der echten ERiC-Bibliothek (nur lokal, ohne Senden)."""
    from app.services.eric import EricClient

    xml = _build()
    with EricClient(ERIC_LIBRARY, tmp_path) as client:
        result = client.process(xml, "UStVA_2026", send=False)
        assert result.ok, (result.message, result.findings)
        assert client.check_tax_number("1096081508187") == 0
        assert client.make_elster_tax_number("181/815/08155", "9181") == elster_tax_number(
            "181/815/08155", "9181"
        )
        broken = xml.replace("<Steuernummer>1096081508187</Steuernummer>", "")
        failed = client.process(broken, "UStVA_2026", send=False)
        assert not failed.ok
        assert any(item.startswith("Steuernummer:") for item in failed.findings)
