# syntax = docker/dockerfile:1.2
FROM python:3.10.6
ENV PYTHONUNBUFFERED 1

# Create all directories
RUN mkdir /app

# Copy code to /app in container
COPY . /app

# Python packages
#RUN --mount=type=cache,target=/root/.cache/pip pip install -r /app/requirements.txt
RUN pip install -r /app/requirements.txt

# GNU Packages - mount and reuse already installed apt pkgs from cache
#RUN --mount=target=/var/lib/apt/lists,type=cache,sharing=locked \
#    --mount=target=/var/cache/apt,type=cache,sharing=locked \
#    rm -f /etc/apt/apt.conf.d/docker-clean \
#    && apt-get update \
#    && apt-get install -yqq --no-install-recommends \
#      socat netcat nano rsync curl tzdata bsdmainutils psmisc net-tools moreutils mosquitto-clients

RUN apt-get update \
    && apt-get install -yqq --no-install-recommends \
      socat netcat nano rsync curl tzdata bsdmainutils psmisc net-tools moreutils mosquitto-clients

# Node.js + Claude Code CLI — used by the dashboard's read-only AI advisor (the
# advisor shells out to `claude`). Container auth is non-interactive, so provide a
# CLAUDE_CODE_OAUTH_TOKEN at runtime (from `claude setup-token`); ADVISOR_AUTH=auto
# then uses it. The advisor is optional — if no token/key is set it simply reports
# that and changes nothing.
RUN curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -yqq --no-install-recommends nodejs \
    && npm install -g @anthropic-ai/claude-code \
    && npm cache clean --force \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

# Specify entry point
CMD ["bash", "/app/entrypoint.sh"]
