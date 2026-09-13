#!/bin/sh
# Write nginx's DNS resolver from whatever DNS this container was given.
#
# Why this exists: nginx resolves a hostname written literally in proxy_pass ONCE, at
# startup, and caches the address for the life of the process. When the api Service's
# address changes -- a namespace recreated, a stack redeployed, a container rescheduled --
# the UI goes on proxying to an address nobody answers. Every /api call then hangs and
# returns 504 while the api itself is perfectly healthy, which reads to a user as "login
# does nothing". Observed live on k3s after the namespace was recreated.
#
# Re-resolution needs two things: a resolver (this file) and the upstream held in a
# variable (see web-nginx.conf.template) -- nginx only re-resolves the latter. Taking the
# nameserver from /etc/resolv.conf keeps one template working on compose, Kubernetes and
# ECS without per-environment configuration.
set -eu

ns=$(awk '/^nameserver/ { print $2; exit }' /etc/resolv.conf 2>/dev/null || true)
# Docker/podman's embedded DNS, if resolv.conf gave us nothing usable.
[ -n "${ns:-}" ] || ns=127.0.0.11
# nginx wants IPv6 resolver addresses bracketed.
case "$ns" in *:*) ns="[$ns]" ;; esac

# conf.d/*.conf is included inside http{}, which is where `resolver` belongs. The short
# TTL matters more than query volume here: it bounds how long a stale address can break
# the UI after a redeploy.
printf 'resolver %s valid=10s ipv6=off;\n' "$ns" > /etc/nginx/conf.d/00-resolver.conf
