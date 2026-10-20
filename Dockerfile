FROM python:3.13-slim
RUN apt-get update && apt-get install -y --no-install-recommends fonts-dejavu-core tzdata && rm -rf /var/lib/apt/lists/*
WORKDIR /app
RUN pip install --no-cache-dir requests==2.32.3 pillow==11.0.0
COPY nice_jawn.py .
ENV PYTHONUNBUFFERED=1 NJ_STATE=/data TZ=America/New_York
VOLUME /data
CMD ["python", "nice_jawn.py", "serve"]
