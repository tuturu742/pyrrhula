# SearXNG with the platform's settings baked in -- for targets that cannot bind-mount
# a config file. Compose mounts docker/searxng-settings.yml directly instead; both
# paths serve the same file.
FROM docker.io/searxng/searxng:latest
COPY docker/searxng-settings.yml /etc/searxng/settings.yml
