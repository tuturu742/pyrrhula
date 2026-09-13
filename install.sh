#!/usr/bin/env bash
# Pyrrhula installer -- one entry point for every supported deployment target.
#
#   ./install.sh compose          # docker/podman compose on this machine
#   ./install.sh k8s              # a Kubernetes cluster (built against k3s)
#   ./install.sh aws              # AWS ECS Fargate via Terraform
#
#   ./install.sh <target> --check # verify prerequisites only, change nothing
#
# Each target's installer is deploy/installers/<target>.sh; the full walkthrough,
# what gets created, and troubleshooting live in docs/install.md.
set -euo pipefail
cd "$(dirname "$0")"

TARGET="${1:-}"
case "$TARGET" in
  compose|k8s|aws)
    shift
    exec "deploy/installers/$TARGET.sh" "$@"
    ;;
  *)
    echo "usage: ./install.sh {compose|k8s|aws} [--check]"
    echo
    echo "  compose  docker or podman on this machine (smallest footprint)"
    echo "  k8s      any Kubernetes cluster; one-command dev install on k3s"
    echo "  aws      ECS Fargate demo stack via Terraform (cloud)"
    echo
    echo "Full guide: docs/install.md"
    exit 64
    ;;
esac
