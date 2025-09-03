# SPDX-License-Identifier: MIT
import ast
import os
import sys
from pathlib import Path

# --- Configuration ---
# Words that, if in a file's content, strongly suggest it's a runnable server.
SERVER_KEYWORDS = {
    "uvicorn.run",
    "app.run",
    "mcp.App",
    "mcp.McpApp",
    "FastAPI",
    "Starlette",
    "http.server",
    "HTTPServer",
}

# Filenames to ignore during the search.
IGNORE_FILENAMES = {
    "__init__.py",
    "setup.py",
    "conftest.py",
    "test_",
    "tests_",
}

# Directories to ignore during the search.
IGNORE_DIRECTORIES = {
    ".venv",
    ".git",
    "__pycache__",
    "tests",
    "docs",
    "examples",
    "scripts",
    ".pytest_cache",
    "dist",
    "build",
    ".tox",
}


def is_likely_server_file(path: Path) -> bool:
    """
    Analyzes a Python file to determine if it's a likely server entry point.

    This function uses a combination of keyword searching and basic Abstract
    Syntax Tree (AST) analysis to identify server patterns without executing the code.

    Args:
        path: The path to the Python file.

    Returns:
        True if the file is a likely server, False otherwise.
    """
    try:
        content = path.read_text(encoding="utf-8", errors="ignore")
        # Fast check: Look for keywords first.
        if not any(keyword in content for keyword in SERVER_KEYWORDS):
            return False

        # Deeper check: Parse the file into an AST to look for patterns.
        tree = ast.parse(content)
        for node in ast.walk(tree):
            # Look for `if __name__ == "__main__":` blocks
            if (
                isinstance(node, ast.If)
                and isinstance(node.test, ast.Compare)
                and isinstance(node.test.left, ast.Name)
                and node.test.left.id == "__name__"
                and isinstance(node.test.ops[0], ast.Eq)
                and isinstance(node.test.comparators[0], ast.Constant)
                and node.test.comparators[0].value == "__main__"
            ):
                # If we find this block, the file is almost certainly runnable.
                return True

            # Look for calls like `uvicorn.run(...)`
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "uvicorn"
                and node.func.attr == "run"
            ):
                return True

        return False
    except (SyntaxError, UnicodeDecodeError, OSError):
        # Ignore files that can't be parsed or read.
        return False


def find_potential_servers(root_dir: Path) -> list[str]:
    """
    Scans a directory for Python files that could be runnable servers.

    Args:
        root_dir: The root directory of the project to scan.

    Returns:
        A list of relative paths to potential server files.
    """
    candidates = []
    for dirpath, dirnames, filenames in os.walk(root_dir):
        # Prune ignored directories to avoid descending into them.
        dirnames[:] = [d for d in dirnames if d not in IGNORE_DIRECTORIES]

        for filename in filenames:
            if not filename.endswith(".py"):
                continue

            if any(ignored in filename for ignored in IGNORE_FILENAMES):
                continue

            full_path = Path(dirpath) / filename
            if is_likely_server_file(full_path):
                # Store the path relative to the root directory.
                relative_path = full_path.relative_to(root_dir)
                candidates.append(str(relative_path))

    return sorted(candidates)


def main():
    """
    Main function to run the script from the command line.
    Expects the target directory as the first argument.
    """
    if len(sys.argv) > 1:
        target_dir = Path(sys.argv[1])
    else:
        target_dir = Path.cwd()

    if not target_dir.is_dir():
        print(f"Error: Directory not found at '{target_dir}'", file=sys.stderr)
        sys.exit(1)

    servers = find_potential_servers(target_dir)

    if servers:
        print("Potential server entry points found:")
        for server in servers:
            print(f"- {server}")
    else:
        print("No potential server entry points were identified.")


if __name__ == "__main__":
    main()
