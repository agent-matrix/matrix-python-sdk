#!/usr/bin/env bash
#
# This script converts all text files in the project from Windows (CRLF)
# to Unix (LF) line endings, which is necessary for scripts to run
# correctly in WSL and other Linux environments.
#
# It automatically excludes common directories like .venv, .git, and __pycache__.

set -euo pipefail

# --- Configuration ---
# Add any other directories you want to exclude to this list.
# The format is a series of '-path ./DIR_NAME -prune -o' arguments.
EXCLUDE_DIRS=(
    -path "./.venv" -prune -o
    -path "./.git" -prune -o
    -path "./__pycache__" -prune
)

# --- Main Logic ---
echo "🔍 Searching for files with Windows (CRLF) line endings..."

# Find all files, excluding the specified directories, and then filter them
# to find only those containing a carriage return ('\r').
# The 'read' command is used to handle filenames with spaces correctly.
FILE_LIST=()
while IFS= read -r file; do
    FILE_LIST+=("$file")
done < <(find . \( "${EXCLUDE_DIRS[@]}" \) -o -type f -print0 | xargs -0 grep -l $'\r')

if [ ${#FILE_LIST[@]} -eq 0 ]; then
    echo "✅ All files already have correct Unix (LF) line endings. Nothing to do."
    exit 0
fi

echo "Found ${#FILE_LIST[@]} file(s) to convert:"
printf "  %s\n" "${FILE_LIST[@]}"
echo

# --- Conversion ---
# Check if dos2unix is installed, as it's the best tool for the job.
if command -v dos2unix &> /dev/null; then
    echo "🚀 Converting files using 'dos2unix'..."
    dos2unix "${FILE_LIST[@]}"
else
    # If dos2unix is not available, use 'sed' as a reliable fallback.
    echo "⚠️ 'dos2unix' not found. Using 'sed' as a fallback."
    echo "🚀 Converting files..."
    for file in "${FILE_LIST[@]}"; do
        sed -i 's/\r$//' "$file"
    done
fi

echo "🎉 Successfully converted ${#FILE_LIST[@]} files."
echo "Your script should now run without the line ending error."