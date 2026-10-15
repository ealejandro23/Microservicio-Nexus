CREATE TABLE IF NOT EXISTS productos (
    sku TEXT PRIMARY KEY,
    nombre TEXT,
    categoria TEXT NOT NULL DEFAULT '',
    marca TEXT,
    precio_lista NUMERIC(12, 2),
    costo NUMERIC(12, 2),
    stock INTEGER NOT NULL DEFAULT 0,
    activo BOOLEAN NOT NULL DEFAULT FALSE,
    fecha_alta DATE
);

CREATE TABLE IF NOT EXISTS usuarios (
    usuario_id TEXT PRIMARY KEY,
    fecha_registro DATE,
    region TEXT,
    comuna TEXT,
    canal_captacion TEXT,
    email_valido BOOLEAN,
    acepta_marketing BOOLEAN,
    edad SMALLINT
);

CREATE TABLE IF NOT EXISTS transacciones (
    pedido_id TEXT NOT NULL,
    linea INTEGER NOT NULL,
    usuario_id TEXT,
    sku TEXT NOT NULL,
    fecha TIMESTAMPTZ,
    cantidad INTEGER NOT NULL DEFAULT 1,
    precio_unitario NUMERIC(12, 2),
    descuento NUMERIC(12, 2),
    total NUMERIC(12, 2),
    canal TEXT,
    estado TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (pedido_id, linea)
);

CREATE TABLE IF NOT EXISTS clickstream (
    evento_id TEXT PRIMARY KEY,
    sesion_id TEXT NOT NULL,
    usuario_id TEXT,
    timestamp TIMESTAMPTZ,
    tipo_evento TEXT NOT NULL,
    sku TEXT,
    termino_busqueda TEXT,
    dispositivo TEXT,
    user_agent TEXT
);

CREATE INDEX IF NOT EXISTS idx_transacciones_usuario_fecha
    ON transacciones (usuario_id, fecha DESC);
CREATE INDEX IF NOT EXISTS idx_transacciones_pedido
    ON transacciones (pedido_id);
CREATE INDEX IF NOT EXISTS idx_transacciones_sku_estado
    ON transacciones (sku, estado);
CREATE INDEX IF NOT EXISTS idx_clickstream_usuario_fecha
    ON clickstream (usuario_id, timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_clickstream_sku_evento
    ON clickstream (sku, tipo_evento);
