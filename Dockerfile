FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY patchpost ./patchpost

# Datenbank in ein Volume legen, damit Abos Neustarts überleben
ENV DB_PATH=/data/patchpost.db
VOLUME /data

CMD ["python", "-m", "patchpost"]
