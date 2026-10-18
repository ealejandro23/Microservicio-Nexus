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


# =============================================================================
# ENDPOINTS ADICIONALES PARA EL FRONT (contrato esperado)
# =============================================================================

IA_SERVICE_URL = os.getenv("IA_SERVICE_URL", "http://ia:8000")


@app.get("/usuarios")
def listar_usuarios() -> list[dict[str, Any]]:
    try:
        with get_connection() as connection:
            with connection.cursor(cursor_factory=RealDictCursor) as cursor:
                cursor.execute("SELECT usuario_id, region FROM usuarios ORDER BY usuario_id")
                rows = cursor.fetchall()
    except psycopg2.Error as exc:
        raise HTTPException(status_code=503, detail="No fue posible consultar usuarios") from exc
    return [{"usuario_id": r["usuario_id"], "region": r["region"]} for r in rows]


@app.get("/categorias")
def listar_categorias() -> list[str]:
    try:
        with get_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT DISTINCT categoria FROM productos WHERE activo IS TRUE ORDER BY categoria")
                rows = cursor.fetchall()
    except psycopg2.Error as exc:
        raise HTTPException(status_code=503, detail="No fue posible consultar categorias") from exc
    return [r[0] for r in rows if r[0]]


@app.get("/usuarios/{usuario_id}/historial")
def historial_usuario(usuario_id: str) -> list[dict[str, Any]]:
    try:
        with get_connection() as connection:
            with connection.cursor(cursor_factory=RealDictCursor) as cursor:
                cursor.execute(
                    "SELECT t.sku, p.nombre AS producto, p.categoria, t.fecha, t.cantidad, t.total, t.estado FROM transacciones t LEFT JOIN productos p ON t.sku = p.sku WHERE t.usuario_id = %s ORDER BY t.fecha DESC LIMIT 20",
                    (usuario_id,),
                )
                rows = cursor.fetchall()
    except psycopg2.Error as exc:
        raise HTTPException(status_code=503, detail="No fue posible consultar historial") from exc
    return [dict(r) for r in rows]


@app.post("/predicciones")
def crear_prediccion(payload: dict[str, Any]) -> dict[str, Any]:
    usuario_id = payload.get("usuario_id")
    categoria = payload.get("categoria")
    if not usuario_id or not categoria:
        raise HTTPException(status_code=422, detail="usuario_id y categoria son requeridos")

    try:
        with get_connection() as connection:
            with connection.cursor(cursor_factory=RealDictCursor) as cursor:
                cursor.execute(
                    "SELECT edad, acepta_marketing FROM usuarios WHERE usuario_id = %s",
                    (usuario_id,),
                )
                user = cursor.fetchone()
    except psycopg2.Error as exc:
        raise HTTPException(status_code=503, detail="No fue posible consultar usuario") from exc

    if user is None:
        raise HTTPException(status_code=404, detail="Usuario no encontrado")

    # Join transacciones with productos to get categoria
    try:
        with get_connection() as connection:
            with connection.cursor() as cursor:
                # Features per-category
                cursor.execute(
                    """SELECT COUNT(*) FROM transacciones t
                       JOIN productos p ON t.sku = p.sku
                       WHERE t.usuario_id = %s AND LOWER(TRIM(t.estado)) = 'pagado'
                         AND LOWER(TRIM(p.categoria)) = LOWER(TRIM(%s))""",
                    (usuario_id, categoria),
                )
                num_compras = cursor.fetchone()[0] or 0
                cursor.execute(
                    """SELECT COALESCE(SUM(t.total), 0) FROM transacciones t
                       JOIN productos p ON t.sku = p.sku
                       WHERE t.usuario_id = %s AND LOWER(TRIM(t.estado)) = 'pagado'
                         AND LOWER(TRIM(p.categoria)) = LOWER(TRIM(%s))""",
                    (usuario_id, categoria),
                )
                total_gastado = float(cursor.fetchone()[0] or 0)
                if total_gastado != total_gastado:
                    total_gastado = 0.0
                cursor.execute(
                    """SELECT MAX(t.fecha) FROM transacciones t
                       JOIN productos p ON t.sku = p.sku
                       WHERE t.usuario_id = %s AND LOWER(TRIM(t.estado)) = 'pagado'
                         AND LOWER(TRIM(p.categoria)) = LOWER(TRIM(%s))""",
                    (usuario_id, categoria),
                )
                ultima = cursor.fetchone()[0]
                # Overall user stats
                cursor.execute(
                    "SELECT COUNT(DISTINCT pedido_id) FROM transacciones WHERE usuario_id = %s AND LOWER(TRIM(estado)) = 'pagado'",
                    (usuario_id,),
                )
                frecuencia = cursor.fetchone()[0] or 0
                cursor.execute(
                    "SELECT COALESCE(AVG(total), 0) FROM transacciones WHERE usuario_id = %s AND LOWER(TRIM(estado)) = 'pagado'",
                    (usuario_id,),
                )
                ticket_promedio_raw = float(cursor.fetchone()[0] or 0)
                if ticket_promedio_raw != ticket_promedio_raw:
                    ticket_promedio_raw = 0.0
    except psycopg2.Error as exc:
        raise HTTPException(status_code=503, detail="No fue posible calcular features") from exc

    import datetime as _dt
    if ultima is not None:
        dias_ultima = (_dt.datetime.now() - ultima).days
    else:
        dias_ultima = 999
    ticket_promedio = ticket_promedio_raw if ticket_promedio_raw > 0 else 0.0

    features = {
        "num_vistas_categoria": 0,
        "num_clics_categoria": 0,
        "num_compras_categoria": num_compras,
        "total_gastado_categoria": total_gastado,
        "dias_ultima_compra": max(dias_ultima, 0),
        "frecuencia_compra": frecuencia,
        "ticket_promedio": ticket_promedio,
        "edad": int(user["edad"] or 30),
        "acepta_marketing": int(user["acepta_marketing"] or 0),
    }

    try:
        import httpx as _httpx

        r = _httpx.post(f"{IA_SERVICE_URL}/predict", json=features, timeout=10.0)
        r.raise_for_status()
        ia_resp = r.json()
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Servicio de IA no disponible: {exc}")

    prob = float(ia_resp.get("prediccion", 0.0))
    version_modelo = ia_resp.get("version_modelo", "1.0.0")

    # =============================================================================
    # CORRECCIÓN DE NEGOCIO: el modelo de IA tiene problemas de calibración y
    # da probabilidades muy bajas incluso con evidencia clara de compra previa.
    # Aplicamos reglas de negocio basadas en el historial real del usuario.
    # =============================================================================

    # Regla 1: Si el usuario ya compró en esta categoría antes → probabilidad alta
    if num_compras > 0:
        prob = max(prob, 0.75)  # Al menos 75% si ya compró en la categoría
        # Compra reciente (menos de 90 días) → aún más alto
        if features["dias_ultima_compra"] < 90:
            prob = max(prob, 0.85)
        # Compra muy reciente (menos de 30 días) → muy alto
        if features["dias_ultima_compra"] < 30:
            prob = max(prob, 0.90)

    # Regla 2: Si tiene vistas o clics en la categoría → interés demostrado
    elif features["num_vistas_categoria"] > 0 or features["num_clics_categoria"] > 0:
        prob = max(prob, 0.45)

    # Regla 3: Si acepta marketing y la probabilidad es baja → pequeño boost
    if int(user["acepta_marketing"] or 0) == 1 and prob < 0.60:
        prob = min(prob * 1.30, 0.95)

    # Asegurar rango válido
    prob = max(0.0, min(1.0, prob))

    try:
        with get_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "INSERT INTO predicciones (usuario_id, sku, categoria, prediccion, propension, version_modelo, tiempo_ms) VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id",
                    (usuario_id, "N/A", categoria, prob, "alta" if prob >= 0.7 else "media" if prob >= 0.4 else "baja", version_modelo, ia_resp.get("tiempo_ms", 0)),
                )
                pred_id = cursor.fetchone()[0]
            connection.commit()
    except psycopg2.Error as exc:
        raise HTTPException(status_code=503, detail="No fue posible guardar prediccion") from exc

    return {
        "probabilidad_compra": prob,
        "prediccion": 1 if prob >= 0.6 else 0,
        "version_modelo": version_modelo,
        "prediccion_id": pred_id,
        "instancia": None,
    }


# =============================================================================
# METRICAS / DASHBOARD
# =============================================================================

@app.get("/metricas")
def metricas_dashboard() -> dict[str, Any]:
    try:
        with get_connection() as connection:
            with connection.cursor(cursor_factory=RealDictCursor) as cursor:
                # Conteos generales
                cursor.execute("SELECT COUNT(*) AS n FROM usuarios")
                total_usuarios = cursor.fetchone()["n"]
                cursor.execute("SELECT COUNT(*) AS n FROM productos")
                total_productos = cursor.fetchone()["n"]
                cursor.execute("SELECT COUNT(*) AS n FROM transacciones")
                total_transacciones = cursor.fetchone()["n"]
                cursor.execute("SELECT COUNT(*) AS n FROM predicciones")
                total_predicciones = cursor.fetchone()["n"]

                # Distribucion de propension
                cursor.execute(
                    "SELECT propension, COUNT(*) AS n FROM predicciones GROUP BY propension"
                )
                propension_dist = {r["propension"]: r["n"] for r in cursor.fetchall()}

                # Usuarios por region (top 10)
                cursor.execute(
                    "SELECT region, COUNT(*) AS n FROM usuarios GROUP BY region ORDER BY n DESC LIMIT 10"
                )
                regiones = [{"region": r["region"], "n": r["n"]} for r in cursor.fetchall()]

                # Categorias de productos
                cursor.execute(
                    "SELECT categoria, COUNT(*) AS n FROM productos WHERE activo IS TRUE GROUP BY categoria ORDER BY n DESC"
                )
                categorias = [{"categoria": r["categoria"], "n": r["n"]} for r in cursor.fetchall()]

                # Transacciones por estado
                cursor.execute(
                    "SELECT estado, COUNT(*) AS n FROM transacciones GROUP BY estado ORDER BY n DESC"
                )
                estados = [{"estado": r["estado"], "n": r["n"]} for r in cursor.fetchall()]

                # Ultimas predicciones
                cursor.execute(
                    "SELECT usuario_id, categoria, prediccion, propension, fecha FROM predicciones ORDER BY fecha DESC LIMIT 10"
                )
                ultimas = [dict(r) for r in cursor.fetchall()]
                for u in ultimas:
                    if u.get("fecha"):
                        u["fecha"] = str(u["fecha"])

                # Promedio de probabilidad
                cursor.execute("SELECT COALESCE(AVG(prediccion), 0) AS avg FROM predicciones")
                avg_prob = float(cursor.fetchone()["avg"] or 0)
                if avg_prob != avg_prob:
                    avg_prob = 0.0

                # Predicciones por propension (completo)
                cursor.execute(
                    "SELECT propension, COUNT(*) AS n FROM predicciones GROUP BY propension"
                )
                propension_all = {r["propension"]: r["n"] for r in cursor.fetchall()}

                # Transacciones por estado (incluye no pagados)
                cursor.execute(
                    "SELECT estado, COUNT(*) AS n FROM transacciones GROUP BY estado ORDER BY n DESC"
                )
                estados_all = [{"estado": r["estado"], "n": r["n"]} for r in cursor.fetchall()]

                # Usuarios con/sin compras
                cursor.execute(
                    "SELECT COUNT(DISTINCT usuario_id) AS n FROM transacciones WHERE LOWER(TRIM(estado)) = 'pagado'"
                )
                usuarios_con_compra = cursor.fetchone()["n"]
                usuarios_sin_compra = total_usuarios - usuarios_con_compra

                # Top productos por transacciones
                cursor.execute(
                    "SELECT p.categoria, COUNT(*) AS n FROM transacciones t JOIN productos p ON t.sku = p.sku GROUP BY p.categoria ORDER BY n DESC LIMIT 8"
                )
                top_cats_tx = [{"categoria": r["categoria"], "n": r["n"]} for r in cursor.fetchall()]

    except psycopg2.Error as exc:
        raise HTTPException(status_code=503, detail="No fue posible consultar metricas") from exc

    return {
        "totales": {
            "usuarios": total_usuarios,
            "productos": total_productos,
            "transacciones": total_transacciones,
            "predicciones": total_predicciones,
        },
        "propension": propension_dist,
        "probabilidad_promedio": round(avg_prob, 4),
        "regiones": regiones,
        "categorias_productos": categorias,
        "estados_transacciones": estados_all,
        "top_categorias_transacciones": top_cats_tx,
        "ultimas_predicciones": ultimas,
        "propension_completa": propension_all,
        "usuarios_con_compra": usuarios_con_compra,
        "usuarios_sin_compra": usuarios_sin_compra,
        "metricas_ia": {},
    }

    # Obtener metricas del modelo de IA
    metricas_ia = {}
    try:
        import httpx as _httpx
        r = _httpx.get(f"{os.getenv('IA_SERVICE_URL', 'http://ia:8000')}/metricas", timeout=5.0)
        if r.status_code == 200:
            metricas_ia = r.json()
    except Exception:
        pass

    # Inyectar metricas_ia en el resultado
    result = {
        "totales": {
            "usuarios": total_usuarios,
            "productos": total_productos,
            "transacciones": total_transacciones,
            "predicciones": total_predicciones,
            "usuarios_con_compra": usuarios_con_compra,
            "usuarios_sin_compra": usuarios_sin_compra,
        },
        "propension": propension_dist,
        "propension_completa": propension_all,
        "probabilidad_promedio": round(avg_prob, 4),
        "regiones": regiones,
        "categorias_productos": categorias,
        "estados_transacciones": estados_all,
        "top_categorias_transacciones": top_cats_tx,
        "ultimas_predicciones": ultimas,
        "metricas_ia": metricas_ia,
    }
    return result


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
