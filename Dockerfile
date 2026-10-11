# One image, two services (API and UI). Python is pinned to 3.12.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .

# The store is rebuilt from data/ at startup; keep it out of the source tree.
RUN mkdir -p /app/store_data
ENV DUCKDB_PATH=/app/store_data/rng.duckdb \
    DATA_DIR=data/primary

EXPOSE 8000 8501
CMD ["uvicorn", "rng.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
