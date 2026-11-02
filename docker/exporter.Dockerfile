FROM python:3.12.11-alpine3.22
WORKDIR /app
COPY llmmonitor /app/llmmonitor
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
USER 65534:65534
EXPOSE 9108
CMD ["python", "-m", "llmmonitor.exporter", "--results", "/data/results", "--runtime", "/data/runtime", "--host", "0.0.0.0"]
