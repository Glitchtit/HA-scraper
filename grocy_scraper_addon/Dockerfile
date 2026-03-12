ARG BUILD_FROM=ghcr.io/home-assistant/amd64-base:latest
FROM $BUILD_FROM

# Install Python3 and pip
RUN apk add --no-cache python3 py3-pip

# Install Python dependencies
COPY requirements.txt /tmp/requirements.txt
RUN pip3 install --no-cache-dir --break-system-packages -r /tmp/requirements.txt

# Copy application
COPY main.py /app/
COPY grocy_scraper/ /app/grocy_scraper/
COPY ingress_server.py /app/

# Copy s6-overlay v3 service definitions
COPY rootfs /

WORKDIR /app
