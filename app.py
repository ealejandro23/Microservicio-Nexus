import os
from typing import Any

import psycopg2
from fastapi import FastAPI, HTTPException, Query
from psycopg2.extras import RealDictCursor

app = FastAPI(
    title="Nexus Retail - Dominio de recomendaciones",
    description="Recomendaciones de productos basadas en compras y navegación.",
    version="1.0.0",
)

RECOMMENDATIONS_QUERY = """
WITH params AS (
    SELECT %s::text AS user_id, %s::text AS context_sku
),
interests AS (
    SELECT sku, SUM(weight) AS weight
    FROM (
        SELECT t.sku, 3.0 * LEAST(GREATEST(t.cantidad, 1), 5) AS weight
        FROM transacciones AS t
        CROSS JOIN params AS p
        WHERE p.user_id IS NOT NULL
          AND t.usuario_id = p.user_id
          AND LOWER(TRIM(t.estado)) = 'pagado'
        UNION ALL
        SELECT c.sku,
               CASE LOWER(TRIM(c.tipo_evento))
                   WHEN 'agregar_carrito' THEN 4.0
                   WHEN 'add_to_cart' THEN 4.0
                   WHEN 'clic' THEN 2.0
                   WHEN 'click' THEN 2.0
                   ELSE 1.0
               END
        FROM clickstream AS c
        CROSS JOIN params AS p
        WHERE p.user_id IS NOT NULL
          AND c.usuario_id = p.user_id
          AND c.sku IS NOT NULL
          AND LOWER(TRIM(c.tipo_evento)) IN
              ('vista', 'view', 'clic', 'click', 'agregar_carrito', 'add_to_cart')
        UNION ALL
        SELECT p.context_sku, 5.0
        FROM params AS p
        WHERE p.context_sku IS NOT NULL
    ) AS signals
    GROUP BY sku
),
co_purchases AS (
    SELECT candidate.sku, SUM(source.weight) AS score
    FROM interests AS source
    JOIN transacciones AS source_order
      ON source_order.sku = source.sku
     AND LOWER(TRIM(source_order.estado)) = 'pagado'
    JOIN transacciones AS candidate
      ON candidate.pedido_id = source_order.pedido_id
     AND candidate.sku <> source.sku
     AND LOWER(TRIM(candidate.estado)) = 'pagado'
    GROUP BY candidate.sku
),
popularity AS (
    SELECT sku, SUM(GREATEST(cantidad, 1)) AS purchases
    FROM transacciones
    WHERE LOWER(TRIM(estado)) = 'pagado'
    GROUP BY sku
)
SELECT p.sku, p.nombre, p.categoria,
       COALESCE(cp.score, 0) + COALESCE(pop.purchases, 0) * 0.01
       + CASE
           WHEN context_product.categoria IS NOT NULL
            AND LOWER(TRIM(p.categoria)) = LOWER(TRIM(context_product.categoria))
           THEN 2.0
           ELSE 0.0
         END AS score
FROM productos AS p
CROSS JOIN params
LEFT JOIN co_purchases AS cp ON cp.sku = p.sku
LEFT JOIN popularity AS pop ON pop.sku = p.sku
LEFT JOIN productos AS context_product ON context_product.sku = params.context_sku
WHERE p.activo IS TRUE
  AND p.stock > 0
  AND (params.context_sku IS NULL OR p.sku <> params.context_sku)
  AND NOT EXISTS (SELECT 1 FROM interests WHERE interests.sku = p.sku)
ORDER BY score DESC, p.sku
LIMIT %s
"""


def get_connection():
    return psycopg2.connect(
        host=os.getenv("DB_HOST", "localhost"),
        port=int(os.getenv("DB_PORT", "5432")),
        dbname=os.getenv("DB_NAME", "nexus_retail"),
        user=os.getenv("DB_USER", "nexus"),
        password=os.getenv("DB_PASSWORD", "nexus"),
        connect_timeout=3,
    )


@app.get("/health")
def health() -> dict[str, str]:
    try:
        with get_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT 1")
                cursor.fetchone()
    except psycopg2.Error as exc:
        raise HTTPException(status_code=503, detail="Base de datos no disponible") from exc
    return {"status": "ok", "database": "connected"}


@app.get("/api/recomendaciones")
def recomendaciones(
    usuario_id: str | None = Query(default=None, min_length=1),
    sku: str | None = Query(default=None, min_length=1),
    limite: int = Query(default=8, ge=1, le=24),
) -> dict[str, Any]:
    try:
        with get_connection() as connection:
            with connection.cursor(cursor_factory=RealDictCursor) as cursor:
                cursor.execute(RECOMMENDATIONS_QUERY, (usuario_id, sku, limite))
                products = cursor.fetchall()
    except psycopg2.Error as exc:
        raise HTTPException(status_code=503, detail="No fue posible consultar recomendaciones") from exc

    return {
        "usuario_id": usuario_id,
        "sku_consultado": sku,
        "recomendaciones": [
            {
                "sku": product["sku"],
                "nombre": product["nombre"],
                "categoria": product["categoria"],
                "score": round(float(product["score"]), 4),
            }
            for product in products
        ],
    }
