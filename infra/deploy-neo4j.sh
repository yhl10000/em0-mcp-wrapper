#!/usr/bin/env bash
# Deploy Neo4j Community Edition to Azure Container Apps
# Usage: NEO4J_PASSWORD=xxx ./infra/deploy-neo4j.sh
#
# Prerequisites:
#   - az CLI logged in (az login)
#   - Existing resource group: rg-mem0-prod
#   - Existing Container Apps Environment (same as mem0-server)

set -euo pipefail

# ─── Config ───
RESOURCE_GROUP="rg-mem0-prod"
LOCATION="westeurope"
NEO4J_CONTAINER_NAME="neo4j-graph"
NEO4J_IMAGE="neo4j:5.26-community"
NEO4J_PASSWORD="${NEO4J_PASSWORD:?Set NEO4J_PASSWORD env var}"
STORAGE_ACCOUNT="stmem0prod"

# Get the Container Apps Environment from existing mem0-server
echo "=== Finding Container Apps Environment ==="
CA_ENV_NAME=$(az containerapp show \
    --name mem0-server \
    --resource-group "$RESOURCE_GROUP" \
    --query "properties.environmentId" -o tsv | rev | cut -d'/' -f1 | rev)
echo "  Environment: $CA_ENV_NAME"

# ─── Step 1: Create container app (CLI flags) ───
echo "=== Creating Neo4j Container App ==="
az containerapp create \
    --name "$NEO4J_CONTAINER_NAME" \
    --resource-group "$RESOURCE_GROUP" \
    --environment "$CA_ENV_NAME" \
    --image "$NEO4J_IMAGE" \
    --cpu 1.0 \
    --memory 2.0Gi \
    --min-replicas 1 \
    --max-replicas 1 \
    --target-port 7687 \
    --ingress internal \
    --transport tcp \
    --secrets "neo4j-auth=neo4j/$NEO4J_PASSWORD" \
    --env-vars \
        "NEO4J_AUTH=secretref:neo4j-auth" \
        "NEO4J_dbms_memory_heap_initial__size=512m" \
        "NEO4J_dbms_memory_heap_max__size=1G" \
        "NEO4J_dbms_memory_pagecache_size=256m"

# ─── Step 2: Add persistent volume via YAML update ───
echo "=== Adding persistent volume ==="

# Get current app config as YAML base
az containerapp show \
    --name "$NEO4J_CONTAINER_NAME" \
    --resource-group "$RESOURCE_GROUP" \
    -o yaml > /tmp/neo4j-current.yaml

# Create update YAML with volume mount
cat > /tmp/neo4j-volume-update.yaml <<'YAMLEOF'
properties:
  template:
    volumes:
      - name: neo4j-data
        storageType: AzureFile
        storageName: neo4jstorage
YAMLEOF

# Update container with volume
az containerapp update \
    --name "$NEO4J_CONTAINER_NAME" \
    --resource-group "$RESOURCE_GROUP" \
    --container-name neo4j \
    --set-env-vars "NEO4J_AUTH=secretref:neo4j-auth" \
    --yaml /tmp/neo4j-volume-update.yaml 2>/dev/null || \
    echo "  Note: Volume mount may need manual config via Azure Portal"

rm -f /tmp/neo4j-current.yaml /tmp/neo4j-volume-update.yaml

# Get the internal FQDN
echo ""
echo "=== Getting Neo4j endpoint ==="
NEO4J_FQDN=$(az containerapp show \
    --name "$NEO4J_CONTAINER_NAME" \
    --resource-group "$RESOURCE_GROUP" \
    --query "properties.configuration.ingress.fqdn" -o tsv)
echo "  Neo4j Bolt URL: bolt://$NEO4J_FQDN:7687"

echo ""
echo "=== Done ==="
echo "Neo4j deployed with:"
echo "  - Password stored as secret (not in env vars)"
echo "  - Internal ingress only (not exposed to internet)"
echo "  - 1 vCPU, 2GB RAM, 1G heap"
echo ""
echo "Next: run NEO4J_PASSWORD=xxx ./infra/update-mem0-config.sh"
