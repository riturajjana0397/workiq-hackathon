# syntax=docker/dockerfile:1.7
FROM python:3.11-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV PIP_DISABLE_PIP_VERSION_CHECK=1

COPY deploy/AgentWebUI/requirements.web.txt /tmp/requirements.txt

RUN --mount=type=cache,target=/root/.cache/pip \
    python -m pip install --upgrade pip && \
    python -m pip install -r /tmp/requirements.txt

COPY agent /app/agent
COPY simulator /app/simulator

EXPOSE 8932

CMD ["python", "agent/subagents/citation_agent.py"]
