FROM python:3.11-slim

# Install FFmpeg
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy API pipeline
COPY api_pipeline/ /app/api_pipeline/

# Install API pipeline dependencies
RUN pip install --no-cache-dir -r /app/api_pipeline/requirements.txt

# Install monolith dependencies (needed for Type 2 monolith simulation and real pipelines)
COPY Comp_Videos/requirements.txt /tmp/monolith-requirements.txt
RUN pip install --no-cache-dir -r /tmp/monolith-requirements.txt || true && rm /tmp/monolith-requirements.txt

# PYTHONPATH: /app so "tvd_pipeline" is importable when volume-mounted
ENV PYTHONPATH=/app

EXPOSE 8000

CMD ["uvicorn", "api_pipeline.server:app", "--host", "0.0.0.0", "--port", "8000"]
