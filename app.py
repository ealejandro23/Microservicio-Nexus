"""Microservicio de dominio: lógica de negocio. Es el ÚNICO que lee/escribe la base.
Orquesta: arma las variables desde la BD -> llama a ms-ia /predict -> guarda la predicción."""
import json
import os

import httpx
import psycopg2
from fastapi import FastAPI, HTTPException
from psycopg2.extras import RealDictCursor
from psycopg2.pool import SimpleConnectionPool
from pydantic import BaseModel

app = FastAPI(title="ms-dominio")
IA_URL = os.getenv("IA_URL", "http://ia:8000")
pool = None


def get_pool():
    
    global pool
    if pool is None:
        pool = SimpleConnectionPool(
            1, 5, host=os.getenv("DB_HOST", "db"), dbname=os.getenv("DB_NAME", "tienda"),
            user=os.getenv("DB_USER", "app"), password=os.getenv("DB_PASSWORD", "app"))
    return pool


def consulta(sql, params=(), uno=False, escribir=False):
    p = get_pool()
    conn = p.getconn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(sql, params)
            res = (cur.fetchone() if uno else cur.fetchall()) if cur.description else None
        if escribir:
            conn.commit()
        return res
    except Exception:
        conn.rollback()
        raise
    finally:
        p.putconn(conn)


class PedirPrediccion(BaseModel):
    usuario_id: int
    categoria: str


@app.get("/health")
def health():
    try:
        consulta("SELECT 1")
    except Exception:
        raise HTTPException(503, "base de datos no disponible")
    return {"status": "ok"}


@app.get("/usuarios/{usuario_id}")
def usuario(usuario_id: int):
    u = consulta("SELECT * FROM usuarios WHERE id=%s", (usuario_id,), uno=True)
    if not u:
        raise HTTPException(404, "usuario no existe")
    return u


@app.get("/productos")
def productos(categoria: str | None = None):
    if categoria:
        return consulta("SELECT * FROM productos WHERE categoria=%s ORDER BY id LIMIT 100", (categoria,))
    return consulta("SELECT * FROM productos ORDER BY id LIMIT 100")


@app.get("/categorias")
def categorias():
    return [r["categoria"] for r in consulta("SELECT DISTINCT categoria FROM productos ORDER BY 1")]


@app.get("/usuarios/{usuario_id}/historial")
def historial(usuario_id: int):
    return consulta(
        """SELECT t.id, t.fecha, p.nombre, p.categoria, t.cantidad, t.monto
           FROM transacciones t JOIN productos p ON p.id=t.producto_id
           WHERE t.usuario_id=%s ORDER BY t.fecha DESC LIMIT 20""", (usuario_id,))


def _predecir(req: PedirPrediccion, guardar: bool = True):
    if not consulta("SELECT 1 FROM usuarios WHERE id=%s", (req.usuario_id,), uno=True):
        raise HTTPException(404, "usuario no existe")

    f = consulta(
        """SELECT count(*)::float AS n_compras_previas,
                  COALESCE(sum(t.monto),0)::float AS gasto_total,
                  COALESCE(sum(CASE WHEN p.categoria=%s THEN t.cantidad END),0)::float AS n_clicks_categoria,
                  COALESCE(CURRENT_DATE - max(t.fecha), 365)::float AS dias_desde_ultima_compra
           FROM transacciones t JOIN productos p ON p.id=t.producto_id
           WHERE t.usuario_id=%s""", (req.categoria, req.usuario_id), uno=True)
    entrada = {"categoria": req.categoria, **f}

    try:
        r = httpx.post(f"{IA_URL}/predict", json=entrada, timeout=5.0)
        r.raise_for_status()
    except httpx.HTTPError:
        raise HTTPException(503, "servicio de IA no disponible")
    ia = r.json()
    if not guardar:
        return {"usuario_id": req.usuario_id, "categoria": req.categoria, **ia}

    fila = consulta(
        """INSERT INTO predicciones (usuario_id, categoria, entrada, probabilidad, prediccion, modelo_version)
           VALUES (%s,%s,%s,%s,%s,%s) RETURNING id, creado_en""",
        (req.usuario_id, req.categoria, json.dumps(entrada), ia["probabilidad"],
         ia["prediccion"], ia["modelo_version"]), uno=True, escribir=True)
    return {"id": fila["id"], "usuario_id": req.usuario_id, "categoria": req.categoria, **ia,
            "creado_en": fila["creado_en"]}


@app.post("/predicciones")
def crear_prediccion(req: PedirPrediccion):
    return _predecir(req)


# Tensión del caso: el stock del ERP llega con hasta 30 min de atraso. Mitigación:
# (1) nunca recomendar con stock <= STOCK_MARGEN (colchón que cubre ventas en la ventana de atraso)
# (2) marcar cada producto con stock_desfasado si su dato supera los 30 min.
STOCK_MARGEN = int(os.getenv("STOCK_MARGEN", "2"))


@app.get("/recomendaciones/{usuario_id}")
def recomendar(usuario_id: int, n: int = 5):
    cats = [r["categoria"] for r in consulta("SELECT DISTINCT categoria FROM productos")]
    puntajes = sorted((_predecir(PedirPrediccion(usuario_id=usuario_id, categoria=c), guardar=False)
                       for c in cats), key=lambda p: -p["probabilidad"])
    if not puntajes:
        raise HTTPException(404, "sin categorías")
    ganadora = _predecir(PedirPrediccion(usuario_id=usuario_id, categoria=puntajes[0]["categoria"]))
    productos = consulta(
        """SELECT id, nombre, categoria, precio, stock,
                  (stock_actualizado_en IS NULL OR stock_actualizado_en < now() - interval '30 minutes')
                  AS stock_desfasado
           FROM productos
           WHERE categoria=%s AND stock > %s
           ORDER BY id LIMIT %s""", (ganadora["categoria"], STOCK_MARGEN, n))
    return {"usuario_id": usuario_id, "categoria_recomendada": ganadora["categoria"],
            "probabilidad": ganadora["probabilidad"], "modelo_version": ganadora["modelo_version"],
            "productos": productos}


@app.get("/predicciones")
def listar(usuario_id: int | None = None, limit: int = 20):
    if usuario_id:
        return consulta("SELECT * FROM predicciones WHERE usuario_id=%s ORDER BY creado_en DESC LIMIT %s",
                        (usuario_id, limit))
    return consulta("SELECT * FROM predicciones ORDER BY creado_en DESC LIMIT %s", (limit,))