FROM python:3.11-slim

WORKDIR /app

# 開発用（docker compose）。テストもこの中で回すので pytest などが入った requirements-dev.txt を入れる。
# 本番用の Dockerfile.prod は requirements.txt だけ
COPY requirements.txt requirements-dev.txt ./
RUN pip install --no-cache-dir -r requirements-dev.txt

COPY . .

CMD ["sh", "-c", "alembic upgrade head && uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload"]
