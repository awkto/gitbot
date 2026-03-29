FROM python:3.12-slim

WORKDIR /app

COPY pyproject.toml .
RUN pip install --no-cache-dir .

COPY version .
COPY gitbot/ gitbot/
COPY scripts/ scripts/

RUN mkdir -p /app/data

EXPOSE 8042

CMD ["uvicorn", "gitbot.server:app", "--host", "0.0.0.0", "--port", "8042"]
