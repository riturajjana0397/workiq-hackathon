FROM python:3.11-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV PIP_DISABLE_PIP_VERSION_CHECK=1

COPY simulator/requirements.txt /tmp/requirements.a2a.txt

RUN pip install --no-cache-dir --upgrade pip && \
	pip install --no-cache-dir -r /tmp/requirements.a2a.txt

COPY simulator /app/simulator

EXPOSE 8920

CMD ["python", "simulator/a2a_server.py"]