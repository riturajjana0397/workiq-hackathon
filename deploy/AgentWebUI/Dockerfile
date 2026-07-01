FROM python:3.11-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

COPY . /app

RUN pip install --no-cache-dir --upgrade pip &&
pip install --no-cache-dir
fastapi uvicorn
agent-framework agent-framework-foundry agent-framework-a2a
azure-identity openai httpx mcp
azure-monitor-opentelemetry

EXPOSE 8000

CMD ["python", "agent/web.py"]