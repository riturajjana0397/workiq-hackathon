# syntax=docker/dockerfile:1.7
FROM python:3.11-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV PIP_DISABLE_PIP_VERSION_CHECK=1

COPY simulator/requirements.txt /tmp/requirements.a2a.txt

RUN --mount=type=cache,target=/root/.cache/pip \
	python -m pip install --upgrade pip && \
	python -m pip install -r /tmp/requirements.a2a.txt

COPY simulator /app/simulator

EXPOSE 8920

CMD ["python", "simulator/a2a_server.py"]