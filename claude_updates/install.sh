#!/bin/zsh
#
# Bloodbank v2.0 Installation Script
# Copies updated files from Claude's output to your bloodbank repo
#

set -e  # Exit on error

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Configuration
BLOODBANK_REPO="${HOME}/code/projects/33GOD/bloodbank"
SOURCE_DIR="/home/claude/bloodbank_updates"

echo "${BLUE}╔════════════════════════════════════════════════╗${NC}"
echo "${BLUE}║  Bloodbank v2.0 Installation Script           ║${NC}"
echo "${BLUE}╔════════════════════════════════════════════════╗${NC}"
echo ""

# Check if repo exists
if [[ ! -d "${BLOODBANK_REPO}" ]]; then
    echo "${RED}✗ Error: Bloodbank repo not found at ${BLOODBANK_REPO}${NC}"
    echo "${YELLOW}Please set BLOODBANK_REPO environment variable to your repo path${NC}"
    exit 1
fi

echo "${GREEN}✓ Found bloodbank repo at ${BLOODBANK_REPO}${NC}"
echo ""

# Check if Redis is running
echo "${BLUE}Checking Redis...${NC}"
if redis-cli ping &> /dev/null; then
    echo "${GREEN}✓ Redis is running${NC}"
else
    echo "${YELLOW}⚠ Warning: Redis is not running${NC}"
    echo "${YELLOW}  Start Redis with: brew services start redis${NC}"
    echo "${YELLOW}  Or: docker run -d -p 6379:6379 redis:7-alpine${NC}"
    echo ""
    read -q "REPLY?Continue anyway? (y/n) "
    echo ""
    if [[ ! $REPLY =~ ^[Yy]$ ]]; then
        exit 1
    fi
fi
echo ""

# Create backup
echo "${BLUE}Creating backup...${NC}"
cd "${BLOODBANK_REPO}"

BACKUP_BRANCH="backup-before-v2.0-$(date +%Y%m%d-%H%M%S)"
git checkout -b "${BACKUP_BRANCH}" 2>/dev/null || true
git add -A 2>/dev/null || true
git commit -m "Backup before v2.0 upgrade" 2>/dev/null || true
echo "${GREEN}✓ Created backup branch: ${BACKUP_BRANCH}${NC}"
echo ""

# Copy files
echo "${BLUE}Copying updated files...${NC}"

# Core files
echo "  → correlation_tracker.py"
cp "${SOURCE_DIR}/correlation_tracker.py" "${BLOODBANK_REPO}/"

echo "  → config.py"
cp "${SOURCE_DIR}/config.py" "${BLOODBANK_REPO}/"

echo "  → rabbit.py"
cp "${SOURCE_DIR}/rabbit.py" "${BLOODBANK_REPO}/"

echo "  → pyproject.toml"
cp "${SOURCE_DIR}/pyproject.toml" "${BLOODBANK_REPO}/"

# Event producers directory
echo "  → event_producers/events.py"
cp "${SOURCE_DIR}/events.py" "${BLOODBANK_REPO}/event_producers/"

echo "  → event_producers/http.py"
cp "${SOURCE_DIR}/http.py" "${BLOODBANK_REPO}/event_producers/"

# Documentation
echo "  → claude_skills/bloodbank_event_publisher/SKILL.md"
mkdir -p "${BLOODBANK_REPO}/claude_skills/bloodbank_event_publisher"
cp "${SOURCE_DIR}/SKILL.md" "${BLOODBANK_REPO}/claude_skills/bloodbank_event_publisher/"

echo "  → docs/MIGRATION_v1_to_v2.md"
mkdir -p "${BLOODBANK_REPO}/docs"
cp "${SOURCE_DIR}/MIGRATION_v1_to_v2.md" "${BLOODBANK_REPO}/docs/"

echo "${GREEN}✓ Files copied successfully${NC}"
echo ""

# Install dependencies
echo "${BLUE}Installing dependencies...${NC}"
cd "${BLOODBANK_REPO}"

if command -v uv &> /dev/null; then
    echo "  Using uv..."
    uv pip install -e .
else
    echo "  Using pip..."
    pip install -e .
fi

echo "${GREEN}✓ Dependencies installed${NC}"
echo ""

# Update .env
echo "${BLUE}Updating .env file...${NC}"
if [[ ! -f "${BLOODBANK_REPO}/.env" ]]; then
    echo "${YELLOW}  Creating new .env file...${NC}"
    cat > "${BLOODBANK_REPO}/.env" <<EOF
# RabbitMQ
RABBIT_URL=amqp://guest:guest@localhost:5672/
EXCHANGE_NAME=amq.topic

# Redis (v2.0)
REDIS_HOST=localhost
REDIS_PORT=6379
REDIS_DB=0
REDIS_PASSWORD=
CORRELATION_TTL_DAYS=30

# HTTP Server
HTTP_HOST=0.0.0.0
HTTP_PORT=8682
EOF
    echo "${GREEN}✓ Created .env file${NC}"
else
    echo "${YELLOW}  .env file exists, checking for Redis settings...${NC}"
    if ! grep -q "REDIS_HOST" "${BLOODBANK_REPO}/.env"; then
        echo "${YELLOW}  Adding Redis settings to existing .env...${NC}"
        cat >> "${BLOODBANK_REPO}/.env" <<EOF

# Redis Configuration (v2.0)
REDIS_HOST=localhost
REDIS_PORT=6379
REDIS_DB=0
REDIS_PASSWORD=
CORRELATION_TTL_DAYS=30
EOF
        echo "${GREEN}✓ Added Redis settings to .env${NC}"
    else
        echo "${GREEN}✓ Redis settings already present in .env${NC}"
    fi
fi
echo ""

# Run tests
echo "${BLUE}Running verification tests...${NC}"

# Test 1: Import correlation tracker
echo -n "  Testing correlation tracker... "
if python -c "from correlation_tracker import CorrelationTracker; CorrelationTracker()" &> /dev/null; then
    echo "${GREEN}✓${NC}"
else
    echo "${RED}✗${NC}"
fi

# Test 2: Import events
echo -n "  Testing events... "
if python -c "from event_producers.events import EventEnvelope, FirefliesTranscriptReadyPayload" &> /dev/null; then
    echo "${GREEN}✓${NC}"
else
    echo "${RED}✗${NC}"
fi

# Test 3: Import publisher
echo -n "  Testing publisher... "
if python -c "from rabbit import Publisher" &> /dev/null; then
    echo "${GREEN}✓${NC}"
else
    echo "${RED}✗${NC}"
fi

# Test 4: Redis connection
echo -n "  Testing Redis connection... "
if python -c "from correlation_tracker import CorrelationTracker; CorrelationTracker()" &> /dev/null; then
    echo "${GREEN}✓${NC}"
else
    echo "${YELLOW}⚠${NC}"
fi

echo ""

# Summary
echo "${BLUE}╔════════════════════════════════════════════════╗${NC}"
echo "${BLUE}║  Installation Complete!                        ║${NC}"
echo "${BLUE}╚════════════════════════════════════════════════╝${NC}"
echo ""
echo "${GREEN}✓ All files copied${NC}"
echo "${GREEN}✓ Dependencies installed${NC}"
echo "${GREEN}✓ Configuration updated${NC}"
echo ""
echo "${YELLOW}Next steps:${NC}"
echo "  1. Read the docs: ${BLOODBANK_REPO}/claude_skills/bloodbank_event_publisher/SKILL.md"
echo "  2. Review migration guide: ${BLOODBANK_REPO}/docs/MIGRATION_v1_to_v2.md"
echo "  3. Start the HTTP server: uvicorn event_producers.http:app --reload --port 8682"
echo "  4. Test correlation: curl http://localhost:8682/healthz"
echo ""
echo "${BLUE}If anything goes wrong, rollback with:${NC}"
echo "  git checkout ${BACKUP_BRANCH}"
echo ""
echo "${GREEN}Happy eventing! 🩸${NC}"
