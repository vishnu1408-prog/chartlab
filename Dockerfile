# ChartLab — local TradingView-style practice terminal (FastAPI backend + static frontend)
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    HOME=/tmp \
    CHARTLAB_INDEX_DIR=/data/kaggle/index \
    CHARTLAB_STOCK_DIR=/data/kaggle/stocks \
    CHARTLAB_DATA_DIR=/data/app \
    CHARTLAB_CUSTOM_DIR=/app/custom_indicators \
    CHARTLAB_FRONTEND_DIR=/app/frontend \
    CHARTLAB_ANGEL_CREDS=/run/secrets/angel.env

WORKDIR /app
COPY requirements.txt requirements-dev.txt ./
RUN pip install --no-cache-dir -r requirements-dev.txt

COPY backend ./backend
COPY frontend ./frontend
COPY custom_indicators ./custom_indicators

# run the test-suite at build time: a broken build never produces an image
RUN cd backend && CHARTLAB_DATA_DIR=/tmp/t PYTHONPATH=.:tests python -m pytest -q tests -p no:cacheprovider \
    && rm -rf /tmp/t

# SmartAPI writes logs/<date>/app.log relative to the working dir; the app runs as the host user
RUN mkdir -m 1777 /app/backend/logs
WORKDIR /app/backend
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=4)"
CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]
