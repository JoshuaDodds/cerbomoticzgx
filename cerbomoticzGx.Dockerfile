# syntax = docker/dockerfile:1.2
FROM python:3.9
ENV PYTHONUNBUFFERED 1

# setup cache for pip don't delete cached apt files after install
ENV PIP_CACHE_DIR=/var/cache/buildkit/pip
RUN mkdir -p $PIP_CACHE_DIR
RUN rm -f /etc/apt/apt.conf.d/docker-clean

# Create all directories
RUN mkdir /app

# Copy code to /app in container
COPY . /app

# Python packages
RUN pip install -r /app/requirements.txt

# GNU Packages - mount and reuse already installed apt pkgs from cache
RUN --mount=type=cache,target=/var/cache/apt \
    apt-get update && apt-get install -yqq --no-install-recommends socat netcat nano rsync curl tzdata \
    bsdmainutils psmisc net-tools moreutils mosquitto-clients

# Specify entry point
CMD ["bash", "/app/entrypoint.sh"]
