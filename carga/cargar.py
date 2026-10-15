import argparse
import csv
import logging
import os
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable

import psycopg2
from psycopg2.extras import execute_values

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
LOGGER = logging.getLogger("carga")

DATE_FORMATS = ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y")
DATETIME_FORMATS = (
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%d/%m/%Y %H:%M:%S",
    "%d/%m/%Y %H:%M",
)


def clean_text(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = value.strip().replace("\x00", "")
    return cleaned or None


def parse_bool(value: str | None) -> bool | None:
    value = clean_text(value)
    if value is None:
        return None
    normalized = value.casefold()
    if normalized in {"true", "1", "si", "sí", "yes", "s"}:
        return True
    if normalized in {"false", "0", "no", "n"}:
        return False
    raise ValueError(f"booleano no reconocido: {value!r}")


def parse_number(value: str | None, parser: Any, field: str) -> Any:
    value = clean_text(value)
    if value is None:
        return None
    try:
        normalized = value
        if parser is Decimal:
            normalized = normalized.replace("$", "").replace(" ", "")
            if "," in normalized and "." in normalized:
                if normalized.rfind(",") > normalized.rfind("."):
                    normalized = normalized.replace(".", "").replace(",", ".")
                else:
                    normalized = normalized.replace(",", "")
            elif "," in normalized:
                decimal_places = len(normalized.rsplit(",", 1)[1])
                normalized = (
                    normalized.replace(",", ".")
                    if decimal_places in {1, 2}
                    else normalized.replace(",", "")
                )
            elif normalized.count(".") > 1:
                normalized = normalized.replace(".", "")
            elif "." in normalized and len(normalized.rsplit(".", 1)[1]) == 3:
                normalized = normalized.replace(".", "")
        parsed = parser(normalized)
        if isinstance(parsed, Decimal) and not parsed.is_finite():
            raise ValueError("el valor debe ser finito")
        return parsed
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"{field} no es un número válido: {value!r}") from exc


def parse_date(value: str | None) -> date | None:
    value = clean_text(value)
    if value is None:
        return None
    for date_format in DATE_FORMATS:
        try:
            return datetime.strptime(value, date_format).date()
        except ValueError:
            pass
    raise ValueError(f"fecha no reconocida: {value!r}")


def parse_timestamp(value: str | None) -> datetime | None:
    value = clean_text(value)
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        parsed = None
        for date_format in DATETIME_FORMATS:
            try:
                parsed = datetime.strptime(value, date_format)
                break
            except ValueError:
                continue
    if parsed is None:
        raise ValueError(f"fecha y hora no reconocidas: {value!r}")
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed


def parse_active(value: str | None) -> bool:
    parsed = parse_bool(value)
    return parsed if parsed is not None else False


def parse_stock(value: str | None, sku: str) -> int:
    if (clean_text(value) or "").casefold() == "error":
        LOGGER.warning("SKU %s tiene stock ERROR; se carga como 0 por seguridad", sku)
        return 0
    parsed = parse_number(value, int, "stock")
    return parsed if parsed is not None else 0


def rows_from_csv(path: Path) -> Iterable[dict[str, str]]:
    try:
        source = path.open("r", encoding="utf-8-sig", newline="")
    except OSError as exc:
        raise RuntimeError(f"No se pudo abrir {path}: {exc}") from exc

    with source:
        reader = csv.DictReader(source)
        if not reader.fieldnames:
            raise ValueError(f"El CSV {path} no tiene encabezados")
        for line_number, row in enumerate(reader, start=2):
            if None in row:
                raise ValueError(f"{path}:{line_number}: cantidad inesperada de columnas")
            yield row


def product_row(row: dict[str, str]) -> tuple[Any, ...]:
    sku = clean_text(row.get("sku"))
    if not sku:
        raise ValueError("sku es obligatorio")
    return (
        sku,
        clean_text(row.get("nombre")),
        clean_text(row.get("categoria")) or "",
        clean_text(row.get("marca")),
        parse_number(row.get("precio_lista"), Decimal, "precio_lista"),
        parse_number(row.get("costo"), Decimal, "costo"),
        parse_stock(row.get("stock"), sku),
        parse_active(row.get("activo")),
        parse_date(row.get("fecha_alta")),
    )


def user_row(row: dict[str, str]) -> tuple[Any, ...]:
    user_id = clean_text(row.get("usuario_id"))
    if not user_id:
        raise ValueError("usuario_id es obligatorio")
    age = parse_number(row.get("edad"), int, "edad")
    if age is not None and not -32768 <= age <= 32767:
        raise ValueError(f"edad fuera del rango SMALLINT: {age}")
    return (
        user_id,
        parse_date(row.get("fecha_registro")),
        clean_text(row.get("region")),
        clean_text(row.get("comuna")),
        clean_text(row.get("canal_captacion")),
        parse_bool(row.get("email_valido")),
        parse_bool(row.get("acepta_marketing")),
        age,
    )


def transaction_row(row: dict[str, str]) -> tuple[Any, ...]:
    order_id = clean_text(row.get("pedido_id"))
    line = parse_number(row.get("linea"), int, "linea")
    sku = clean_text(row.get("sku"))
    if not order_id or line is None or not sku:
        raise ValueError("pedido_id, linea y sku son obligatorios")
    return (
        order_id,
        line,
        clean_text(row.get("usuario_id")),
        sku,
        parse_timestamp(row.get("fecha")),
        parse_number(row.get("cantidad"), int, "cantidad") or 1,
        parse_number(row.get("precio_unitario"), Decimal, "precio_unitario"),
        parse_number(row.get("descuento"), Decimal, "descuento"),
        parse_number(row.get("total"), Decimal, "total"),
        clean_text(row.get("canal")),
        (clean_text(row.get("estado")) or "").casefold(),
    )


def clickstream_row(row: dict[str, str]) -> tuple[Any, ...]:
    event_id = clean_text(row.get("evento_id"))
    session_id = clean_text(row.get("sesion_id"))
    event_type = clean_text(row.get("tipo_evento"))
    if not event_id or not session_id or not event_type:
        raise ValueError("evento_id, sesion_id y tipo_evento son obligatorios")
    return (
        event_id,
        session_id,
        clean_text(row.get("usuario_id")),
        parse_timestamp(row.get("timestamp")),
        event_type.casefold(),
        clean_text(row.get("sku")),
        clean_text(row.get("termino_busqueda")),
        clean_text(row.get("dispositivo")),
        clean_text(row.get("user_agent")),
    )


TABLES = (
    (
        "productos.csv",
        product_row,
        """INSERT INTO productos
           (sku, nombre, categoria, marca, precio_lista, costo, stock, activo, fecha_alta)
           VALUES %s
           ON CONFLICT (sku) DO UPDATE SET
               nombre = EXCLUDED.nombre, categoria = EXCLUDED.categoria,
               marca = EXCLUDED.marca, precio_lista = EXCLUDED.precio_lista,
               costo = EXCLUDED.costo, stock = EXCLUDED.stock,
               activo = EXCLUDED.activo, fecha_alta = EXCLUDED.fecha_alta""",
    ),
    (
        "usuarios.csv",
        user_row,
        """INSERT INTO usuarios
           (usuario_id, fecha_registro, region, comuna, canal_captacion,
            email_valido, acepta_marketing, edad)
           VALUES %s
           ON CONFLICT (usuario_id) DO UPDATE SET
               fecha_registro = EXCLUDED.fecha_registro, region = EXCLUDED.region,
               comuna = EXCLUDED.comuna, canal_captacion = EXCLUDED.canal_captacion,
               email_valido = EXCLUDED.email_valido,
               acepta_marketing = EXCLUDED.acepta_marketing, edad = EXCLUDED.edad""",
    ),
    (
        "transacciones.csv",
        transaction_row,
        """INSERT INTO transacciones
           (pedido_id, linea, usuario_id, sku, fecha, cantidad, precio_unitario,
            descuento, total, canal, estado)
           VALUES %s
           ON CONFLICT (pedido_id, linea) DO UPDATE SET
               usuario_id = EXCLUDED.usuario_id, sku = EXCLUDED.sku,
               fecha = EXCLUDED.fecha, cantidad = EXCLUDED.cantidad,
               precio_unitario = EXCLUDED.precio_unitario, descuento = EXCLUDED.descuento,
               total = EXCLUDED.total, canal = EXCLUDED.canal, estado = EXCLUDED.estado""",
    ),
    (
        "clickstream.csv",
        clickstream_row,
        """INSERT INTO clickstream
           (evento_id, sesion_id, usuario_id, timestamp, tipo_evento, sku,
            termino_busqueda, dispositivo, user_agent)
           VALUES %s
           ON CONFLICT (evento_id) DO UPDATE SET
               sesion_id = EXCLUDED.sesion_id, usuario_id = EXCLUDED.usuario_id,
               timestamp = EXCLUDED.timestamp, tipo_evento = EXCLUDED.tipo_evento,
               sku = EXCLUDED.sku, termino_busqueda = EXCLUDED.termino_busqueda,
               dispositivo = EXCLUDED.dispositivo, user_agent = EXCLUDED.user_agent""",
    ),
)


def load_file(cursor: Any, data_dir: Path, filename: str, converter: Any, query: str) -> int:
    batch: dict[tuple[Any, ...], tuple[Any, ...]] = {}
    count = 0
    for line_number, row in enumerate(rows_from_csv(data_dir / filename), start=2):
        try:
            values = converter(row)
        except (ValueError, InvalidOperation) as exc:
            raise ValueError(f"{filename}:{line_number}: {exc}") from exc
        key = values[:2] if filename == "transacciones.csv" else values[:1]
        batch[key] = values
        if len(batch) >= 500:
            execute_values(cursor, query, list(batch.values()))
            count += len(batch)
            batch.clear()
    if batch:
        execute_values(cursor, query, list(batch.values()))
        count += len(batch)
    return count


def load_data(data_dir: Path) -> dict[str, int]:
    counts = {}
    connection_options = {
        "host": os.getenv("DB_HOST", "localhost"),
        "port": int(os.getenv("DB_PORT", "5432")),
        "dbname": os.getenv("DB_NAME", "nexus_retail"),
        "user": os.getenv("DB_USER", "nexus"),
        "password": os.getenv("DB_PASSWORD", "nexus"),
        "connect_timeout": 5,
    }
    with psycopg2.connect(**connection_options) as connection:
        with connection.cursor() as cursor:
            for filename, converter, query in TABLES:
                counts[filename] = load_file(cursor, data_dir, filename, converter, query)
    return counts


def main() -> None:
    parser = argparse.ArgumentParser(description="Carga los CSV de Nexus Retail en PostgreSQL.")
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=Path(os.getenv("DATA_DIR", Path(__file__).resolve().parent / "datos")),
        help="Carpeta con productos.csv, usuarios.csv, transacciones.csv y clickstream.csv",
    )
    args = parser.parse_args()
    counts = load_data(args.data_dir.expanduser().resolve())
    for filename, count in counts.items():
        LOGGER.info("%s: %d filas procesadas", filename, count)


if __name__ == "__main__":
    main()
