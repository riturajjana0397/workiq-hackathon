# syntax=docker/dockerfile:1.7
FROM python:3.11-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV PIP_DISABLE_PIP_VERSION_CHECK=1

COPY deploy/AgentWebUI/requirements.web.txt /tmp/requirements.web.txt

RUN --mount=type=cache,target=/root/.cache/pip \
    python -m pip install --upgrade pip && \
    python -m pip install -r /tmp/requirements.web.txt

# Copy only runtime code needed by the web agent to keep context small and
# preserve cache for dependency layers.
COPY agent /app/agent
COPY simulator /app/simulator

EXPOSE 8000

CMD ["python", "agent/web.py"]