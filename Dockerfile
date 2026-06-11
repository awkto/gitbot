FROM python:3.12-slim

# git for the SDK engine's clone workspaces (implement workflow)
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY pyproject.toml .
RUN pip install --no-cache-dir .

COPY version .
COPY gitbot/ gitbot/
COPY scripts/ scripts/

RUN mkdir -p /app/data

EXPOSE 8042

CMD ["uvicorn", "gitbot.server:app", "--host", "0.0.0.0", "--port", "8042"]
