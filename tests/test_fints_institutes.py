"""FinTS-Bankenliste: Import, BLZ-Abfrage, URL-Allowlist und Produktkennung."""

from __future__ import annotations

import base64
import io
from datetime import date

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app import create_app
from app.auth import hash_api_token, hash_password
from app.services import fints_sync
from app.services.fints_institutes import (
    FinTSInstituteError,
    fints_institute_list_status,
    fints_urls_for_blz,
    import_fints_institutes,
    lookup_fints_institutes,
    parse_fints_institute_csv,
    validate_fints_url,
)
from app.services.fints_sync import (
    FinTSSyncError,
    create_fints_connection,
    start_fints_sync,
)
from domain.models import Account, Base, Company, FinTSConnection, FinTSInstitute, Tenant, User

HEADER = (
    "Nr.;BLZ;BIC;Institut;Ort;RZ;Organisation;HBCI-Zugang DNS;HBCI- Zugang     IP-Adresse;"
    "HBCI-Version;DDV;RDH-1;RDH-2;RDH-3;RDH-4;RDH-5;RDH-6;RDH-7;RDH-8;RDH-9;RDH-10;RAH-7;"
    "RAH-9;RAH-10;PIN/TAN-Zugang URL;Version;Datum letzte Änderung"
)
EMPTY_HBCI = ";" * 16


def _row(nr, blz, bic, name, city, url, changed=""):
    return (
        f"{nr};{blz};{bic};{name};{city};eigenes Rechenzentrum;BdB;{EMPTY_HBCI};"
        f"{url};FinTS V3.0;{changed}"
    )


SAMPLE_ROWS = [
    _row(
        2,
        "10010010",
        "PBNKDEFFXXX",
        "Postbank",
        "Berlin",
        "https://hbci.example-bank.de/fints",
        "04.02.2022",
    ),
    _row(
        3,
        "10020890",
        "HYVEDEMM488",
        "Beispielbank Köln",
        "Berlin",
        "https://fints.beispiel.de/bank/hbci",
    ),
    # Gleiche BLZ und URL, anderer Ort → Duplikat.
    _row(
        4,
        "10020890",
        "HYVEDEMM488",
        "Beispielbank Köln",
        "Kleinmachnow",
        "https://fints.beispiel.de/bank/hbci/",
    ),
    # Zweite URL für dieselbe BLZ.
    _row(5, "20030000", "MULTDEFFXXX", "Mehrfachbank", "Hamburg", "https://a.mehrfach.de/fints"),
    _row(6, "20030000", "MULTDEFFXXX", "Mehrfachbank", "Hamburg", "https://b.mehrfach.de/fints"),
    # Ohne PIN/TAN-Zugang (bzw. ohne https).
    _row(7, "30040000", "OHNEDEFFXXX", "Ohne-URL-Bank", "München", ""),
    _row(8, "30050000", "", "HTTP-Bank", "Bonn", "http://unsicher.example.de/fints"),
    # Ungültige BLZ.
    _row(9, "1234", "", "Kaputt", "Nirgendwo", "https://kaputt.example.de"),
]


def _csv(rows=SAMPLE_ROWS, encoding="cp1252") -> bytes:
    # Wie im Original: nummerierte Leerzeilen am Ende.
    trailer = ["9999" + ";" * 26, ";" * 26]
    return ("\r\n".join([HEADER, *rows, *trailer]) + "\r\n").encode(encoding)


@pytest.fixture()
def session() -> Session:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as test_session:
        yield test_session


def _seed_company(session: Session) -> tuple[Company, Account]:
    tenant = Tenant(name="Bankenliste")
    company = Company(tenant=tenant, name="Bankenliste GmbH", currency_code="EUR")
    session.add_all([tenant, company])
    session.flush()
    bank = Account(
        tenant_id=tenant.id,
        company_id=company.id,
        code="1200",
        name="Bank",
        account_type="asset",
    )
    session.add(bank)
    session.commit()
    return company, bank


def _connection(session, company, bank, *, name="Konto", blz="10010010", url=""):
    return create_fints_connection(
        session=session,
        company_id=company.id,
        bank_account_id=bank.id,
        name=name,
        blz=blz,
        login="kunde1",
        fints_url=url,
        changed_by="tester",
    )


# ---------------------------------------------------------------------------
# Import


def test_parse_dk_list_cp1252_dedups_and_skips_invalid_rows():
    rows, report = parse_fints_institute_csv(_csv())

    assert report.total_rows == 8
    assert report.skipped_rows == 1
    assert report.duplicate_rows == 1
    assert report.imported_rows == 6
    assert report.with_url == 4
    assert report.distinct_blz == 5
    assert "1234" in report.errors[0]

    postbank = rows[0]
    assert postbank["bic"] == "PBNKDEFFXXX"
    assert postbank["fints_url"] == "https://hbci.example-bank.de/fints"
    assert postbank["fints_version"] == "FinTS V3.0"
    assert postbank["last_changed"] == date(2022, 2, 4)
    assert rows[1]["name"] == "Beispielbank Köln"
    # http-URLs werden nicht übernommen.
    assert next(row for row in rows if row["blz"] == "30050000")["fints_url"] is None


def test_parse_accepts_utf8_with_bom():
    rows, _ = parse_fints_institute_csv(b"\xef\xbb\xbf" + _csv(encoding="utf-8"))
    assert rows[1]["name"] == "Beispielbank Köln"


@pytest.mark.parametrize(
    ("content", "message"),
    [
        (b"", "leer"),
        (b"PK\x03\x04xlsx-inhalt", "Excel"),
        ("Konto;Name\n1200;Bank\n".encode(), "keine FinTS-Bankenliste"),
        (_csv(rows=[_row(9, "1234", "", "Kaputt", "X", "https://x.de")]), "keine gültigen"),
    ],
)
def test_parse_rejects_invalid_files(content, message):
    with pytest.raises(FinTSInstituteError, match=message):
        parse_fints_institute_csv(content)


def test_import_replaces_list_and_reports_status(session: Session):
    assert fints_institute_list_status(session).count == 0

    import_fints_institutes(session=session, content=_csv(), changed_by="tester")
    status = fints_institute_list_status(session)
    assert (status.count, status.with_url) == (6, 4)
    assert status.imported_at is not None

    import_fints_institutes(session=session, content=_csv(rows=SAMPLE_ROWS[:1]), changed_by="t")
    assert fints_institute_list_status(session).count == 1
    assert session.execute(select(FinTSInstitute.blz)).scalars().all() == ["10010010"]


def test_lookup_orders_entries_with_url_first(session: Session):
    import_fints_institutes(session=session, content=_csv(), changed_by="tester")

    assert [item.city for item in lookup_fints_institutes(session, "10020890")] == ["Berlin"]
    assert fints_urls_for_blz(session, "20030000") == [
        "https://a.mehrfach.de/fints",
        "https://b.mehrfach.de/fints",
    ]
    assert fints_urls_for_blz(session, "30040000") == []
    assert lookup_fints_institutes(session, "99999999") == []
    with pytest.raises(FinTSInstituteError, match="8 Ziffern"):
        lookup_fints_institutes(session, "12")


# ---------------------------------------------------------------------------
# URL-Prüfung / Allowlist (Review S5)


@pytest.mark.parametrize(
    "url",
    [
        "http://fints.example.de/fints",
        "https://localhost/fints",
        "https://127.0.0.1/fints",
        "https://10.0.0.5/fints",
        "https://192.168.0.180/fints",
        "https://[::1]/fints",
        "https://169.254.169.254/latest",
        "https://intranet/fints",
        "https://nas.local/fints",
        "https://user:pw@fints.example.de/fints",
    ],
)
def test_validate_rejects_insecure_or_internal_urls(session: Session, url: str):
    with pytest.raises(FinTSInstituteError):
        validate_fints_url(session, url)


def test_validate_without_list_accepts_public_https(session: Session):
    url = "https://fints.irgendeine-bank.de/fints30"
    assert validate_fints_url(session, url) == url


def test_validate_with_list_requires_listed_url(session: Session):
    import_fints_institutes(session=session, content=_csv(), changed_by="tester")

    # Normalisiert: Groß-/Kleinschreibung des Hosts und abschließender Schrägstrich egal.
    assert validate_fints_url(session, "https://FINTS.beispiel.de/bank/hbci/")
    with pytest.raises(FinTSInstituteError, match="nicht in der importierten"):
        validate_fints_url(session, "https://angreifer.example.com/fints")


# ---------------------------------------------------------------------------
# Bankzugang anlegen / Abruf


def test_create_connection_resolves_url_from_list(session: Session):
    company, bank = _seed_company(session)
    import_fints_institutes(session=session, content=_csv(), changed_by="tester")

    connection = _connection(session, company, bank, blz="10020890")
    assert connection.fints_url == "https://fints.beispiel.de/bank/hbci"

    with pytest.raises(FinTSSyncError, match="mehrere URLs"):
        _connection(session, company, bank, name="Mehrfach", blz="20030000")
    explicit = _connection(
        session, company, bank, name="Mehrfach", blz="20030000", url="https://b.mehrfach.de/fints"
    )
    assert explicit.fints_url == "https://b.mehrfach.de/fints"

    with pytest.raises(FinTSSyncError, match="nicht .*in der"):
        _connection(session, company, bank, name="Ohne", blz="30040000")
    with pytest.raises(FinTSSyncError, match="nicht in der importierten"):
        _connection(
            session, company, bank, name="Fremd", blz="10010010", url="https://evil.example.com/x"
        )


def test_create_connection_without_list_needs_url(session: Session):
    company, bank = _seed_company(session)
    with pytest.raises(FinTSSyncError, match="BLZ steht nicht"):
        _connection(session, company, bank)
    with pytest.raises(FinTSSyncError, match="privates Netz"):
        _connection(session, company, bank, url="https://192.168.1.10/fints")


def test_sync_refuses_url_missing_from_newer_list(session: Session, monkeypatch):
    company, bank = _seed_company(session)
    connection = _connection(session, company, bank, url="https://alt.example-bank.de/fints")
    import_fints_institutes(session=session, content=_csv(), changed_by="tester")

    def fail_build(*args, **kwargs):
        raise AssertionError("PIN darf nicht an eine unbekannte URL gehen")

    monkeypatch.setattr(fints_sync, "_build_client", fail_build)
    with pytest.raises(FinTSSyncError, match="nicht in der importierten"):
        start_fints_sync(
            session=session,
            connection_id=connection.id,
            pin="1234",
            product_id="TESTPRODUKT00000000000001",
            changed_by="tester",
        )


@pytest.mark.parametrize("product_id", ["X" * 26, "ABC DEF"])
def test_sync_rejects_invalid_product_id(session: Session, product_id: str):
    company, bank = _seed_company(session)
    connection = _connection(session, company, bank, url="https://fints.example-bank.de/fints")
    with pytest.raises(FinTSSyncError, match="25 Zeichen"):
        start_fints_sync(
            session=session,
            connection_id=connection.id,
            pin="1234",
            product_id=product_id,
            changed_by="tester",
        )


def test_startup_warns_on_product_id_length(tmp_path, caplog):
    with caplog.at_level("WARNING"):
        create_app(
            {
                "TESTING": True,
                "DATABASE_URL": f"sqlite+pysqlite:///{tmp_path / 'warn.db'}",
                "FINTS_PRODUCT_ID": "ZU-KURZ",
            }
        )
    assert "FINTS_PRODUCT_ID hat 7 statt 25 Zeichen" in caplog.text


# ---------------------------------------------------------------------------
# API / Web


def _app(tmp_path, **overrides):
    config = {
        "TESTING": True,
        "DATABASE_URL": f"sqlite+pysqlite:///{tmp_path / 'institute.db'}",
        "SECRET_KEY": "test",
    }
    config.update(overrides)
    app = create_app(config)
    with app.extensions["db_session_factory"]() as db:
        tenant = Tenant(name="API")
        company = Company(tenant=tenant, name="API GmbH", currency_code="EUR")
        db.add_all([tenant, company])
        db.flush()
        bank = Account(
            tenant_id=tenant.id,
            company_id=company.id,
            code="1200",
            name="Bank",
            account_type="asset",
        )
        global_admin = User(
            username="admin",
            password_hash=hash_password("geheim123"),
            role="Admin",
            api_token_hash=hash_api_token("obk_global"),
            api_token_last4="obal",
        )
        tenant_admin = User(
            username="mandantenadmin",
            password_hash=hash_password("geheim123"),
            role="Admin",
            tenant_id=tenant.id,
            api_token_hash=hash_api_token("obk_tenant"),
            api_token_last4="nant",
        )
        db.add_all([bank, global_admin, tenant_admin])
        db.commit()
        ids = (company.id, bank.id)
    return app, ids


def test_api_import_lookup_and_create_without_url(tmp_path):
    app, (company_id, bank_id) = _app(tmp_path, API_REQUIRE_AUTH=True)
    client = app.test_client()
    tenant_auth = {"Authorization": "Bearer obk_tenant"}
    global_auth = {"Authorization": "Bearer obk_global"}
    payload = {"content_base64": base64.b64encode(_csv()).decode()}

    denied = client.post("/api/v1/fints-institutes/import", json=payload, headers=tenant_auth)
    assert denied.status_code == 403

    imported = client.post("/api/v1/fints-institutes/import", json=payload, headers=global_auth)
    assert imported.status_code == 200, imported.get_json()
    body = imported.get_json()
    assert body["report"]["imported_rows"] == 6
    assert body["list"]["with_url"] == 4

    bad = client.post(
        "/api/v1/fints-institutes/import", json={"content_base64": "%%%"}, headers=global_auth
    )
    assert bad.status_code == 400

    status = client.get("/api/v1/fints-institutes", headers=tenant_auth).get_json()
    assert status["list"]["count"] == 6 and "institutes" not in status

    lookup = client.get("/api/v1/fints-institutes?blz=20030000", headers=tenant_auth).get_json()
    assert lookup["fints_urls"] == ["https://a.mehrfach.de/fints", "https://b.mehrfach.de/fints"]
    assert lookup["institutes"][0]["bic"] == "MULTDEFFXXX"
    assert client.get("/api/v1/fints-institutes?blz=1", headers=tenant_auth).status_code == 422

    created = client.post(
        "/api/v1/fints-connections",
        json={
            "company_id": company_id,
            "bank_account_id": bank_id,
            "name": "Postbank",
            "blz": "10010010",
            "login": "kunde1",
        },
        headers=tenant_auth,
    )
    assert created.status_code == 201, created.get_json()
    assert created.get_json()["fints_url"] == "https://hbci.example-bank.de/fints"


def test_web_import_requires_global_admin_and_lookup_route(tmp_path):
    app, (company_id, _) = _app(tmp_path)

    with app.test_client() as client:
        client.post("/auth/login", data={"username": "mandantenadmin", "password": "geheim123"})
        client.post(
            "/verwaltung/fints-bankenliste",
            data={"fints_institute_csv": (io.BytesIO(_csv()), "fints_institute.csv")},
            content_type="multipart/form-data",
        )
    with app.extensions["db_session_factory"]() as db:
        assert fints_institute_list_status(db).count == 0

    with app.test_client() as client:
        client.post("/auth/login", data={"username": "admin", "password": "geheim123"})
        response = client.post(
            "/verwaltung/fints-bankenliste",
            data={"fints_institute_csv": (io.BytesIO(_csv()), "fints_institute.csv")},
            content_type="multipart/form-data",
            follow_redirects=True,
        )
        assert "FinTS-Bankenliste importiert: 6 Einträge" in response.get_data(as_text=True)

        lookup = client.get("/bank/fints/institut?blz=10020890").get_json()
        assert lookup["fints_urls"] == ["https://fints.beispiel.de/bank/hbci"]
        assert lookup["institutes"][0]["name"] == "Beispielbank Köln"

        page = client.get(f"/bank?company_id={company_id}").get_data(as_text=True)
        assert 'data-fints-lookup="/bank/fints/institut"' in page
        assert "leer lassen = automatisch" in page

    with app.extensions["db_session_factory"]() as db:
        assert db.execute(select(FinTSConnection)).first() is None


def test_cli_imports_list(tmp_path):
    app, _ = _app(tmp_path)
    csv_path = tmp_path / "fints_institute.csv"
    csv_path.write_bytes(_csv())

    result = app.test_cli_runner().invoke(
        args=["import-fints-institute", "--csv-path", str(csv_path)]
    )
    assert result.exit_code == 0, result.output
    assert "einträge=6" in result.output
