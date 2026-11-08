import pathlib

import pytest

from financeapp import create_app
from financeapp.db import get_db

FIXTURES = pathlib.Path(__file__).parent / "fixtures"


@pytest.fixture
def app(tmp_path):
    return create_app({"TESTING": True, "DATABASE": str(tmp_path / "test.db")})


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def db(app):
    with app.app_context():
        yield get_db()


def fixture_text(name):
    return (FIXTURES / name).read_text()


def make_account(db, name, bank, kind="checking"):
    cur = db.execute(
        "INSERT INTO accounts (name, bank, kind) VALUES (?, ?, ?)", (name, bank, kind)
    )
    db.commit()
    return cur.lastrowid
