FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV COOKIE_SECURE=1

WORKDIR /app

COPY pyproject.toml requirements.txt ./
COPY src ./src
COPY web ./web
COPY web_server.py ./web_server.py

RUN python -m pip install --no-cache-dir --upgrade pip \
    && python -m pip install --no-cache-dir -e .

EXPOSE 8000

CMD ["python", "web_server.py", "serve", "--host", "0.0.0.0"]
