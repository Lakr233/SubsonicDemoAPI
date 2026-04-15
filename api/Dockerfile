FROM python:3.12-slim

WORKDIR /app
COPY api/server.py /app/server.py
COPY albums /app/albums

ENV HOST=0.0.0.0
ENV PORT=8080
ENV LIBRARY_ROOT=/app/albums
ENV LIBRARY_MANIFEST_PATH=/app/albums/library_manifest.json

CMD ["python", "/app/server.py"]
