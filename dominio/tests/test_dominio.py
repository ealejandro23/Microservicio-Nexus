import unittest
from unittest.mock import MagicMock, patch

import psycopg2
from fastapi import HTTPException
from fastapi.testclient import TestClient

from dominio.app import app, recomendaciones
from carga.cargar import parse_date, parse_number, parse_stock, parse_timestamp
from decimal import Decimal


class DominioTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)

    @patch("dominio.app.get_connection")
    def test_health_reports_connected_database(self, get_connection):
        connection = MagicMock()
        get_connection.return_value.__enter__.return_value = connection

        response = self.client.get("/health")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok", "database": "connected"})
        connection.cursor.return_value.__enter__.return_value.execute.assert_called_once_with("SELECT 1")

    @patch("dominio.app.get_connection", side_effect=psycopg2.OperationalError("connection refused"))
    def test_health_reports_database_unavailable(self, _get_connection):
        response = self.client.get("/health")

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["detail"], "Base de datos no disponible")

    @patch("dominio.app.get_connection")
    def test_recommendations_return_ranked_products(self, get_connection):
        cursor = MagicMock()
        cursor.fetchall.return_value = [
            {"sku": "NX-10001", "nombre": "Notebook", "categoria": "Notebooks", "score": 6.5},
        ]
        connection = MagicMock()
        connection.cursor.return_value.__enter__.return_value = cursor
        get_connection.return_value.__enter__.return_value = connection

        response = self.client.get(
            "/api/recomendaciones",
            params={"usuario_id": "U10001", "sku": "NX-10002", "limite": 4},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            {
                "usuario_id": "U10001",
                "sku_consultado": "NX-10002",
                "recomendaciones": [
                    {
                        "sku": "NX-10001",
                        "nombre": "Notebook",
                        "categoria": "Notebooks",
                        "score": 6.5,
                    }
                ],
            },
        )
        self.assertEqual(cursor.execute.call_args.args[1], ("U10001", "NX-10002", 4))

    def test_recommendation_limit_is_validated(self):
        response = self.client.get("/api/recomendaciones?limite=25")
        self.assertEqual(response.status_code, 422)

    def test_loader_normalizes_chilean_currency_and_date_formats(self):
        self.assertEqual(parse_number("$19.929", Decimal, "precio"), Decimal("19929"))
        self.assertEqual(parse_date("22-11-2024").isoformat(), "2024-11-22")
        self.assertEqual(parse_stock("ERROR", "NX-10001"), 0)
        self.assertEqual(
            parse_timestamp("28/07/2026 15:55").isoformat(),
            "2026-07-28T15:55:00+00:00",
        )

    @patch("dominio.app.get_connection", side_effect=psycopg2.OperationalError("connection refused"))
    def test_recommendations_report_database_unavailable(self, _get_connection):
        with self.assertRaises(HTTPException) as raised:
            recomendaciones(usuario_id=None, sku=None, limite=8)

        self.assertEqual(raised.exception.status_code, 503)


if __name__ == "__main__":
    unittest.main()
