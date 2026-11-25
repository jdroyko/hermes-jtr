import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import create_app  # noqa: E402


@pytest.fixture()
def app():
    fd, path = tempfile.mkstemp(suffix=".sqlite3")
    os.close(fd)
    os.remove(path)
    app = create_app({"DATABASE": path, "TESTING": True, "TESTING_NO_CSRF": True, "SECRET_KEY": "test"})
    yield app
    if os.path.exists(path):
        os.remove(path)


@pytest.fixture()
def client(app):
    return app.test_client()


def login(client, username):
    return client.post("/login", data={"username": username, "password": "Password123!"})


@pytest.fixture()
def as_user(app):
    def _make(username):
        c = app.test_client()
        r = login(c, username)
        assert r.status_code == 302, "login failed for %s" % username
        return c
    return _make
