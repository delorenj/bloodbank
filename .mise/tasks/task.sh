#!/bin/bash
#MISE description = "Stow existing task and create a new empty task"

# Find TASK.md files
echo "🔍 Searching for TASK.md files..."

# Find all TASK.md files
TASK_FILES=$(find . -name "TASK.md" -type f | grep -v "\.git")

if [ -z "$TASK_FILES" ]; then
    echo "No TASK.md files found."
    exit 0
fi

# Count found TASK.md files
TASK_COUNT=$(echo "$TASK_FILES" | wc -l)

if [ "$TASK_COUNT" -gt 1 ]; then
    echo "⚠️  Warning: Found $TASK_COUNT TASK.md files:"
    echo "$TASK_FILES"
    echo ""
    read -p "Continue? (Y/n): " -n 1 -r
    echo
    if [[ ! $REPLY =~ ^[Yy]$ ]] && [ -n "$REPLY" ]; then
        echo "Operation cancelled."
        exit 0
    fi
    echo "Continuing with task management..."
fi

# Create date and time directories
DATE_DIR=$(date +%Y-%m-%d)
TIME_SUFFIX=$(date +%H%M)
TARGET_DIR="docs/threads/$DATE_DIR"

# Create target directory if it doesn't exist
mkdir -p "$TARGET_DIR"

# Move each TASK.md file to the organized location
for TASK_FILE in $TASK_FILES; do
    # Skip the root TASK.md if we're going to create a new one
    if [ "$TASK_FILE" = "./TASK.md" ]; then
        # For the root TASK.md, preserve the content
        if [ -f "$TASK_FILE" ]; then
            # Generate unique filename
            TASK_BASENAME=$(basename "$TASK_FILE")
            
            NEW_FILENAME="$TIME_SUFFIX-${TASK_BASENAME%.md}.md"
            
            # Move the file
            mv "$TASK_FILE" "$TARGET_DIR/$NEW_FILENAME"
            echo "📄 Moved: $TASK_FILE -> $TARGET_DIR/$NEW_FILENAME"
        fi
    fi
done

# Create a new TASK.md at the project root
cat > TASK.md << 'EOF'
# TASK

EOF

# Open the new TASK.md in nvim
echo "📝 Opening new TASK.md in nvim..."
nvim TASK.md

echo "✅ Script completed successfully"