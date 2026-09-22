#!/usr/bin/env bash
# Pyrrhula installer -- one entry point for every supported deployment target.
#
#   ./install.sh compose          # docker/podman compose on this machine
#   ./install.sh k8s              # a Kubernetes cluster (built against k3s)
#   ./install.sh aws              # AWS ECS Fargate via Terraform
#
#   ./install.sh <target> --check # verify prerequisites only, change nothing
#   ./install.sh <target> --purge # delete the deployment AND its data, then install
#
# Tenancy (default: single). Single-tenant means nobody types an organization name to
# sign in -- the right shape for one person or one team. It is a flag over the same
# multi-tenant core, not a different build, so rerunning with the other flag switches
# an existing deployment:
#
#   ./install.sh compose                  # single-tenant (default)
#   ./install.sh compose --multi-tenant   # host several organizations
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
    echo "usage: ./install.sh {compose|k8s|aws} [--single-tenant|--multi-tenant] [--check] [--purge]"
    echo
    echo "  compose  docker or podman on this machine (smallest footprint)"
    echo "  k8s      any Kubernetes cluster; one-command dev install on k3s"
    echo "  aws      ECS Fargate demo stack via Terraform (cloud)"
    echo
    echo "  --single-tenant  one organization, no organization field at login (default)"
    echo "  --multi-tenant   several organizations, each named at login"
    echo "  Rerun with the other flag to switch; nothing is migrated either way."
    echo
    echo "  --check          verify prerequisites, change nothing"
    echo "  --purge          DELETE the deployment and its data first, then install"
    echo "                   (compose: volumes + generated .env; k8s: both namespaces)"
    echo
    echo "Full guide: docs/install.md"
    exit 64
    ;;
esac
