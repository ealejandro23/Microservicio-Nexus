from unittest.mock import patch

from fastapi.testclient import TestClient

import app as m

c = TestClient(m.app)


def test_health_ok():
    with patch.object(m, "consulta", return_value=[{"?column?": 1}]):
        assert c.get("/health").json() == {"status": "ok"}


def test_health_sin_bd():
    with patch.object(m, "consulta", side_effect=Exception("down")):
        assert c.get("/health").status_code == 503


def test_usuario_inexistente():
    with patch.object(m, "consulta", return_value=None):
        assert c.get("/usuarios/999").status_code == 404