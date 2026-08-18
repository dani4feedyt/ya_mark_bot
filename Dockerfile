FROM python:3.12-slim

WORKDIR /app

ENV PYTHONUNBUFFERED=1

RUN echo "deb http://deb.debian.org/debian trixie main non-free non-free-firmware" > /etc/apt/sources.list.d/non-free.list && \
    echo "deb http://deb.debian.org/debian-security trixie-security main non-free non-free-firmware" >> /etc/apt/sources.list.d/non-free.list && \
    apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    intel-media-va-driver-non-free \
    vainfo \
    ffmpeg \
    git \
    curl \
    aria2 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
RUN curl -fsSL https://deno.land/install.sh | sh -s -- -y \
    && mv /root/.deno/bin/deno /usr/local/bin/deno \

COPY . .

CMD ["python", "main.py"]