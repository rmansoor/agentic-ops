FROM python:3.12-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends git nodejs npm \
 && rm -rf /var/lib/apt/lists/*

RUN useradd --create-home --uid 10001 app
WORKDIR /app

COPY pyproject.toml README.md ./
COPY agentic_ops ./agentic_ops
RUN pip install --no-cache-dir ".[all]"

COPY policy.yml ./policy.yml
COPY semgrep ./semgrep

USER app
ENV PYTHONUNBUFFERED=1 \
    SEMGREP_SEND_METRICS=off
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/healthz').status==200 else 1)"
CMD ["agentic-ops", "serve", "--host", "0.0.0.0", "--port", "8080"]
