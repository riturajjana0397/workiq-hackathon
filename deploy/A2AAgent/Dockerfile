FROM python:3.11-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

COPY . /app

RUN pip install --no-cache-dir --upgrade pip &&
pip install --no-cache-dir -r requirements.txt

EXPOSE 8920

CMD ["python", "simulator/a2a_server.py"]