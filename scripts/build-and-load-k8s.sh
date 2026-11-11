#!/usr/bin/env bash
# Builds the demo images with Docker and loads them straight into MicroK8s
# (no registry needed), then deploys the workload.
#
#   ./scripts/build-and-load-k8s.sh
set -euo pipefail
cd "$(dirname "$0")/.."

IMAGES="frontend order-service payment-service inventory-service traffic-generator"

echo "==> Building images with Docker Compose"
docker compose --profile loadgen build

echo "==> Importing images into MicroK8s (containerd)"
for name in $IMAGES; do
  echo "    shop-demo/$name:1.0.0"
  docker save "shop-demo/$name:1.0.0" | microk8s ctr image import -
done

echo "==> Deploying to MicroK8s"
microk8s kubectl apply -f kubernetes/namespace.yaml
microk8s kubectl apply -f kubernetes/workload.yaml
microk8s kubectl -n shop-demo rollout status deployment --timeout=180s

echo
microk8s kubectl -n shop-demo get pods -o wide
echo
echo "Frontend (NodePort): http://127.0.0.1:30080"
