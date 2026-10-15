# =============================================================================
# DOCKERFILE - MICROSERVICIO DE DOMINIO
# =============================================================================
# Imagen base Python slim para linux/amd64
# =============================================================================

FROM python:3.11-slim

# Establecer directorio de trabajo
WORKDIR /app

# Instalar dependencias del sistema
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Copiar requirements e instalar dependencias Python
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copiar código de la aplicación
COPY app.py .

# Exponer puerto
EXPOSE 8000

# Health check
HEALTHCHECK --interval=30s --timeout=10s --retries=3 \
    CMD curl -f http://localhost:8000/health || exit 1

# Comando para ejecutar la API
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
