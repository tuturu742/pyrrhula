#!/bin/sh
# Runs before the nginx image's envsubst step (as /docker-entrypoint.d/12-tls.sh) and
# switches the server to HTTPS when PYRRHULA_TLS asks for it:
#
#   PYRRHULA_TLS=""             plain http, the default template (nothing happens here)
#   PYRRHULA_TLS=provided       certificates mounted at /etc/nginx/certs/{fullchain,privkey}.pem
#   PYRRHULA_TLS=self-signed    the same paths, generated here on first start if absent
#
# A self-signed certificate is generated ONCE: mount /etc/nginx/certs as a volume and it
# survives recreates, so the exception a browser was told to accept stays valid. Browsers
# still warn the first time. That is the honest cost of no certificate authority; the
# point is that the page is then a secure context (what a Godot web build insists on) and
# a password never crosses the network in clear.
set -eu

mode="${PYRRHULA_TLS:-}"
[ -z "$mode" ] && exit 0

certs=/etc/nginx/certs
name="${PYRRHULA_TLS_SERVER_NAME:-localhost}"

case "$mode" in
  provided) ;;
  self-signed)
    if [ ! -s "$certs/fullchain.pem" ] || [ ! -s "$certs/privkey.pem" ]; then
      mkdir -p "$certs"
      # An address is named as an IP SAN, anything else as a DNS one; browsers ignore the
      # CN and match only the SAN.
      if echo "$name" | grep -Eq '^[0-9]+(\.[0-9]+){3}$'; then
        san="IP:$name,DNS:localhost,IP:127.0.0.1"
      else
        san="DNS:$name,DNS:localhost,IP:127.0.0.1"
      fi
      openssl req -x509 -newkey rsa:2048 -nodes -days 825 -sha256 \
        -keyout "$certs/privkey.pem" -out "$certs/fullchain.pem" \
        -subj "/CN=$name/O=Pyrrhula self-signed" -addext "subjectAltName=$san" 2>/dev/null
      chmod 600 "$certs/privkey.pem"
      echo "12-tls.sh: generated a self-signed certificate for $name ($san)"
    fi
    ;;
  *)
    echo "12-tls.sh: PYRRHULA_TLS must be empty, 'provided' or 'self-signed', not '$mode'" >&2
    exit 1
    ;;
esac

if [ ! -s "$certs/fullchain.pem" ] || [ ! -s "$certs/privkey.pem" ]; then
  echo "12-tls.sh: PYRRHULA_TLS=$mode but $certs/fullchain.pem and privkey.pem are missing" >&2
  exit 1
fi

template=/etc/nginx/templates/default.conf.template
cp /etc/nginx/tls/default.conf.template "$template"
# The release stacks publish ONE port, mapped to the container's 80. There the TLS server
# listens on 80 itself and the plain-http redirect server goes; plain http that still
# arrives on that port is redirected by the server's own `error_page 497`.
if [ "${PYRRHULA_TLS_LISTEN:-443}" = "80" ]; then
  sed -i '/# BEGIN plain-http redirect/,/# END plain-http redirect/d' "$template"
  sed -i 's/^    listen 443 ssl;$/    listen 80 ssl;/' "$template"
fi
if [ "$mode" = "self-signed" ]; then
  # HSTS forbids clicking through a certificate warning, so on a self-signed certificate
  # it would lock every browser out of the deployment for a year. Leave it to a real one.
  sed -i '/Strict-Transport-Security/d' "$template"
fi
