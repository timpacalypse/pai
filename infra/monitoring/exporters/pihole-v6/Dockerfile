FROM python:3.12-slim
WORKDIR /app
RUN pip install --no-cache-dir prometheus_client
COPY exporter.py .
EXPOSE 9620
CMD ["python", "-u", "exporter.py"]
