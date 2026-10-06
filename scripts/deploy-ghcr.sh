#!/bin/bash

# Deploy script for GitHub Container Registry (ghcr.io)
# Usage: ./scripts/deploy-ghcr.sh [version]
# Ohne Argument: APP_VERSION Patch +1 (1.4.13 → 1.4.14)
# Example: ./scripts/deploy-ghcr.sh 1.5.0
#
# Reihenfolge:
#   1. Version eintragen
#   2. Commit + Push
#   3. Merge nach main
#   4. Image bauen + pushen

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

SETTINGS_FILE="$REPO_ROOT/dashboard/settings.py"
GIT_REMOTE="${DEPLOY_GIT_REMOTE:-origin}"
MAIN_BRANCH="main"

read_app_version() {
    grep -oE 'APP_VERSION[[:space:]]*=[[:space:]]*"[^"]+"' "$SETTINGS_FILE" | head -1 | sed -E 's/.*"([^"]+)".*/\1/'
}

bump_patch() {
    local current="$1"
    local major minor patch
    IFS='.' read -r major minor patch <<< "$current"
    if [[ -z "$major" || -z "$minor" || -z "$patch" ]]; then
        echo "1.0.0"
        return
    fi
    echo "${major}.${minor}.$((patch + 1))"
}

CURRENT_APP_VERSION="$(read_app_version)"
if [[ -z "$CURRENT_APP_VERSION" ]]; then
    echo -e "${RED}Error: APP_VERSION in dashboard/settings.py nicht gefunden${NC}"
    exit 1
fi

# App-Version: $1 als 1.4.14 oder v1.4.14, sonst Patch +1
if [[ -n "${1:-}" && "$1" =~ ^v?[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
    APP_VERSION="${1#v}"
else
    if [[ -n "${1:-}" ]]; then
        echo -e "${YELLOW}Hinweis: \"$1\" ist keine Versionsnummer (z.B. 1.4.14). Zähle Patch hoch.${NC}"
    fi
    APP_VERSION="$(bump_patch "$CURRENT_APP_VERSION")"
fi
VERSION="$APP_VERSION"

echo -e "${GREEN}=== Deploy to GitHub Container Registry ===${NC}"
echo -e "Image: ${FULL_IMAGE}"
echo -e "App-Version: ${CURRENT_APP_VERSION} → ${APP_VERSION}"
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

SOURCE_BRANCH="$(git branch --show-current)"
if [[ -z "$SOURCE_BRANCH" ]]; then
    echo -e "${RED}✗ Detached HEAD — erst auf einen Branch wechseln.${NC}"
    exit 1
fi
if [[ -n "$(git status --porcelain)" ]]; then
    echo -e "${RED}✗ Ungespeicherte Änderungen. Erst alles committen, dann erneut deployen.${NC}"
    git status --short
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

# ── Step 1: Version eintragen ──
echo ""
echo -e "${YELLOW}Step 1: App-Version eintragen (${CURRENT_APP_VERSION} → ${APP_VERSION})...${NC}"
sed -i "s/^APP_VERSION = \".*\"/APP_VERSION = \"${APP_VERSION}\"/" "$SETTINGS_FILE"
echo -e "${GREEN}✓ settings.py: APP_VERSION = \"${APP_VERSION}\"${NC}"

# ── Step 2: Commit + Push ──
echo ""
echo -e "${YELLOW}Step 2: Commit + Push...${NC}"
if git diff --quiet -- "$SETTINGS_FILE"; then
    echo -e "${GREEN}✓ APP_VERSION war schon ${APP_VERSION}${NC}"
else
    git add "$SETTINGS_FILE"
    git commit -m "Bump App-Version auf ${APP_VERSION}"
    git push "$GIT_REMOTE" "$SOURCE_BRANCH"
    echo -e "${GREEN}✓ ${SOURCE_BRANCH} gepusht${NC}"
fi

# ── Step 3: Merge nach main ──
echo ""
echo -e "${YELLOW}Step 3: Branch ${MAIN_BRANCH} aktualisieren...${NC}"
git fetch "$GIT_REMOTE"
git checkout "$MAIN_BRANCH"
git pull --ff-only "$GIT_REMOTE" "$MAIN_BRANCH"
if [[ "$SOURCE_BRANCH" != "$MAIN_BRANCH" ]]; then
    git merge "$SOURCE_BRANCH" --no-edit
fi
git push "$GIT_REMOTE" "$MAIN_BRANCH"
git checkout "$SOURCE_BRANCH"
echo -e "${GREEN}✓ ${MAIN_BRANCH} enthält ${SOURCE_BRANCH} und ist gepusht${NC}"

# ── Step 4: Image bauen ──
echo ""
echo -e "${YELLOW}Step 4: Login ghcr.io...${NC}"
echo "$GHCR_TOKEN" | docker login ghcr.io -u "$OWNER" --password-stdin
echo -e "${GREEN}✓ Login successful${NC}"

echo ""
echo -e "${YELLOW}Step 5: Docker-Image bauen...${NC}"
docker build -t "${FULL_IMAGE}:${VERSION}" -t "${FULL_IMAGE}:latest" .
echo -e "${GREEN}✓ Image built${NC}"

echo ""
echo -e "${YELLOW}Step 6: Images nach ghcr.io pushen...${NC}"
docker push "${FULL_IMAGE}:${VERSION}"
docker push "${FULL_IMAGE}:latest"
echo -e "${GREEN}✓ Images pushed${NC}"

echo ""
echo -e "${YELLOW}Step 7: Alte Images aufräumen...${NC}"
OLD_IMAGES=$(docker images "${FULL_IMAGE}" --format "{{.ID}} {{.Tag}}" | grep -v "latest" | tail -n +3 | awk '{print $1}')
if [ -n "$OLD_IMAGES" ]; then
    echo "$OLD_IMAGES" | xargs docker rmi -f 2>/dev/null || true
    echo -e "${GREEN}✓ Alte Images entfernt${NC}"
else
    echo -e "${GREEN}✓ Keine alten Images zum Aufräumen${NC}"
fi
docker image prune -f 2>/dev/null || true
docker builder prune -f 2>/dev/null || true
echo -e "${GREEN}✓ Build-Cache aufgeräumt${NC}"

# Summary
echo ""
echo -e "${GREEN}=== Deployment Complete ===${NC}"
echo ""
echo -e "${GREEN}Neue Version: ${APP_VERSION}${NC}"
echo ""
echo "Images available at:"
echo "  ${FULL_IMAGE}:${VERSION}"
echo "  ${FULL_IMAGE}:latest"
echo ""
echo "Tito kann jetzt update.sh ausführen (holt main + Image)."
echo "Manuell: docker pull ${FULL_IMAGE}:latest"
