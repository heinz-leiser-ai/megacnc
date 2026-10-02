#!/bin/bash

# Deploy script for GitHub Container Registry (ghcr.io)
# Usage: ./scripts/deploy-ghcr.sh [version]
# Example: ./scripts/deploy-ghcr.sh v1.0.0

set -e

# Configuration
REGISTRY="ghcr.io"
OWNER="heinz-leiser-ai"
IMAGE_NAME="megacnc"
FULL_IMAGE="${REGISTRY}/${OWNER}/${IMAGE_NAME}"

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

# Repo-Root (Skript liegt in scripts/)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"

# Version: Docker-Tag ohne Leerzeichen. Freitext -> Zeitstempel.
if [[ -n "${1:-}" && "$1" =~ ^[A-Za-z0-9._-]+$ ]]; then
    VERSION="$1"
else
    if [[ -n "${1:-}" ]]; then
        echo -e "${YELLOW}Hinweis: \"$1\" ist kein gültiger Image-Tag. Nutze Zeitstempel.${NC}"
    fi
    VERSION="$(date +%Y%m%d-%H%M%S)"
fi

echo -e "${GREEN}=== Deploy to GitHub Container Registry ===${NC}"
echo -e "Image: ${FULL_IMAGE}"
echo -e "Version: ${VERSION}"
echo ""

# Check for GHCR_TOKEN environment variable
if [ -z "$GHCR_TOKEN" ]; then
    echo -e "${RED}Error: GHCR_TOKEN environment variable not set${NC}"
    echo ""
    echo "To set up:"
    echo "1. Create a Personal Access Token at GitHub:"
    echo "   https://github.com/settings/tokens/new"
    echo "   Required scopes: write:packages, read:packages, delete:packages"
    echo ""
    echo "2. Export the token:"
    echo "   export GHCR_TOKEN='your_token_here'"
    echo ""
    echo "3. Or add to ~/.bashrc for persistence"
    exit 1
fi

# Optional: Create DB backup before deploy
read -p "Create database backup before deploy? (y/N): " CREATE_BACKUP
if [[ "$CREATE_BACKUP" =~ ^[Yy]$ ]]; then
    echo -e "${YELLOW}Creating database backup...${NC}"
    mkdir -p backups
    BACKUP_FILE="backups/db_backup_predeploy_$(date +%Y%m%d_%H%M%S).sql"
    
    if docker-compose exec -T db pg_dump -U postgres mcccnc > "$BACKUP_FILE" 2>/dev/null; then
        gzip "$BACKUP_FILE"
        echo -e "${GREEN}✓ Backup created: ${BACKUP_FILE}.gz${NC}"
    else
        echo -e "${YELLOW}⚠ Could not create backup (containers not running?)${NC}"
    fi
fi

# Login to ghcr.io
echo -e "${YELLOW}Step 1: Logging in to ghcr.io...${NC}"
echo "$GHCR_TOKEN" | docker login ghcr.io -u "$OWNER" --password-stdin
echo -e "${GREEN}✓ Login successful${NC}"

# Build image
echo ""
echo -e "${YELLOW}Step 2: Building Docker image...${NC}"
docker build -t "${FULL_IMAGE}:${VERSION}" -t "${FULL_IMAGE}:latest" .
echo -e "${GREEN}✓ Image built${NC}"

# Push images
echo ""
echo -e "${YELLOW}Step 3: Pushing images to ghcr.io...${NC}"
docker push "${FULL_IMAGE}:${VERSION}"
docker push "${FULL_IMAGE}:latest"
echo -e "${GREEN}✓ Images pushed${NC}"

# Cleanup: alte Images aufräumen, nur letzte 2 Versionen behalten
echo ""
echo -e "${YELLOW}Step 4: Alte Images aufräumen...${NC}"

OLD_IMAGES=$(docker images "${FULL_IMAGE}" --format "{{.ID}} {{.Tag}}" | grep -v "latest" | tail -n +3 | awk '{print $1}')
if [ -n "$OLD_IMAGES" ]; then
    echo "$OLD_IMAGES" | xargs docker rmi -f 2>/dev/null || true
    echo -e "${GREEN}✓ Alte Images entfernt${NC}"
else
    echo -e "${GREEN}✓ Keine alten Images zum Aufräumen${NC}"
fi

# Dangling Images und Build-Cache entfernen
docker image prune -f 2>/dev/null || true
docker builder prune -f 2>/dev/null || true
echo -e "${GREEN}✓ Build-Cache aufgeräumt${NC}"

# ── Step 5: developer → main (Tito: git pull origin main) ──
echo ""
echo -e "${YELLOW}Step 5: Branch main aktualisieren...${NC}"
GIT_REMOTE="${DEPLOY_GIT_REMOTE:-origin}"
MAIN_BRANCH="main"
SOURCE_BRANCH="$(git branch --show-current)"

if [[ -n "$(git status --porcelain)" ]]; then
    echo -e "${RED}✗ Ungespeicherte Änderungen — Merge nach ${MAIN_BRANCH} übersprungen.${NC}"
    echo -e "${YELLOW}  Erst committen, dann erneut deployen oder manuell mergen.${NC}"
elif [[ -z "$SOURCE_BRANCH" ]]; then
    echo -e "${RED}✗ Detached HEAD — Merge nach ${MAIN_BRANCH} übersprungen.${NC}"
else
    set +e
    git fetch "$GIT_REMOTE" \
        && git checkout "$MAIN_BRANCH" \
        && git pull --ff-only "$GIT_REMOTE" "$MAIN_BRANCH" \
        && { [[ "$SOURCE_BRANCH" == "$MAIN_BRANCH" ]] || git merge "$SOURCE_BRANCH" --no-edit; } \
        && git push "$GIT_REMOTE" "$MAIN_BRANCH"
    MERGE_STATUS=$?
    git checkout "$SOURCE_BRANCH" >/dev/null 2>&1
    set -e
    if [[ $MERGE_STATUS -ne 0 ]]; then
        echo -e "${RED}✗ Merge/Push nach ${MAIN_BRANCH} fehlgeschlagen. Image ist trotzdem auf ghcr.io.${NC}"
        exit 1
    fi
    echo -e "${GREEN}✓ ${MAIN_BRANCH} enthält jetzt ${SOURCE_BRANCH} und ist gepusht${NC}"
fi

# Summary
echo ""
echo -e "${GREEN}=== Deployment Complete ===${NC}"
echo ""
echo "Images available at:"
echo "  ${FULL_IMAGE}:${VERSION}"
echo "  ${FULL_IMAGE}:latest"
echo ""
echo "Tito: Update-Skript wie gewohnt (holt main + Image)."
echo "Manuell: docker pull ${FULL_IMAGE}:latest"
