# ======================================
# Heatai Project: Dockerfile
# ======================================
# Purpose:
#   Containerized Python 3.12 app for controlling Protherm Skat boiler via EBUSD + Home Assistant.
#   Installs dependencies once (requests, pyyaml).
#   Expects heatai.py + config.yaml mounted at runtime as volumes.
#
# Usage:
#   docker-compose up -d
# ======================================

FROM python:3.12-slim

# Set working directory inside container
WORKDIR /app

# Install dependencies once (cached layer)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Default command — script mounted via volume
CMD ["python", "heatai.py"]


#AUTO RELOAD

#COPY dev_entrypoint.sh /app/dev_entrypoint.sh
#RUN chmod +x /app/dev_entrypoint.sh

#CMD ["./dev_entrypoint.sh"]
