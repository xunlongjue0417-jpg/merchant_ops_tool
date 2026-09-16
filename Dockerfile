FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py ./
COPY static ./static
RUN useradd --create-home appuser && chown -R appuser /app

ENV PYTHONUNBUFFERED=1
EXPOSE 10000
USER appuser

CMD ["python", "app.py"]
