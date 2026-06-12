FROM python:3.12-slim

# git for the SDK engine's clone workspaces (implement workflow),
# glab for GitLab operations the native tools don't cover
ARG GLAB_VERSION=1.102.0
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates curl \
    && curl -fsSL "https://gitlab.com/gitlab-org/cli/-/releases/v${GLAB_VERSION}/downloads/glab_${GLAB_VERSION}_linux_amd64.tar.gz" \
       | tar -xz -C /usr/local --strip-components=0 bin/glab \
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
