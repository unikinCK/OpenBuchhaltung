from app import create_app


def test_index_page_loads():
    app = create_app()
    app.config.update(TESTING=True)

    client = app.test_client()
    response = client.get("/")

    assert response.status_code == 200
    assert b"OpenBuchhaltung" in response.data


def test_login_with_valid_credentials():
    app = create_app()
    app.config.update(TESTING=True)

    client = app.test_client()
    response = client.post(
        "/auth/login",
        data={"username": "admin", "password": "admin"},
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert b"Login erfolgreich" in response.data
