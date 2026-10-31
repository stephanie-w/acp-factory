# Justfile
# Unified task runner for puzzles-extractor

# Configuration
set shell := ["bash", "-uc"]

# ========== DEFAULT ==========

# Show all available commands (default)
default:
    @just --list

# ========== DEVELOPMENT SET-UP ==========

# Set up development environment (sync deps and install pre-commit)
init: install-dev pre-commit-install
    @echo "Development environment initialized. ✅"

# Install package and dev dependencies using uv
install-dev:
    uv sync --all-groups

# Install package (production only)
install:
    uv sync --no-dev

# Enter development shell
shell:
    uv shell

# ========== CODE QUALITY & LINTING ==========

# [Check] Run all quality checks (formatting, linting, types)
check: format-check lint typecheck
    @echo "All quality checks passed! ✅"

# [Fix] Automatically fix formatting and linting issues
fix:
    uv run ruff format
    uv run ruff check --fix --unsafe-fixes

# Format code with ruff
format:
    uv run ruff format

# Check formatting without making changes
format-check:
    uv run ruff format --check

# Run linting with ruff
lint:
    uv run ruff check

# Run type checking with ty
typecheck:
    uv run ty check

# Run pre-commit on all files
pre-commit:
    uv run pre-commit run --all-files

# Install pre-commit hooks
pre-commit-install:
    uv run pre-commit install --install-hooks

# Update pre-commit hooks to latest versions
pre-commit-update:
    uv run pre-commit autoupdate

# Run pre-commit on all files (including uncommitted)
pre-commit-run-all:
    uv run pre-commit run --all-files --show-diff-on-failure

# ========== TESTING ==========

# [Test] Run tests with pytest
test:
    uv run pytest -v

# Run all tests (including coverage)
test-all: test-cov
    @echo "Full test suite completed! ✅"

# Run tests with coverage report
test-cov:
    uv run pytest -v --cov=src --cov-report=term-missing --cov-report=html

# Run fast tests only (skips slow markers)
test-fast:
    uv run pytest -v -m "not slow"

# ========== SECURITY ==========

# Run security checks with bandit
security-check:
    uv run bandit -r src -x tests

# Audit dependencies for known vulnerabilities
audit:
    uv export --format requirements --no-hashes | uv pip audit -

# ========== DEPENDENCIES ==========

# Update dependencies to latest compatible versions
update:
    uv lock --upgrade

# Update all dependencies to latest versions
update-all:
    uv lock --upgrade-package "*"

# ========== DOCUMENTATION ==========

# Build documentation
docs:
    uv run mkdocs build

# Serve documentation locally
docs-serve:
    uv run mkdocs serve

# ========== BUILD & CLEAN ==========

# Clean all build artifacts, caches, and temporary files
clean:
    rm -rf build/ dist/ .pytest_cache/ .ty_cache/ .ruff_cache/ htmlcov/ .coverage
    find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
    find . -type f -name "*.pyc" -delete

# Build package using hatchling
build: clean
    uv build

# ========== CI & RELEASE ==========

# Run full CI suite (checks + tests)
ci: check test
    @echo "CI suite passed! 🚀"

# Prepare release (dry run)
release-dry: ci build
    @echo "Release ready! Run 'just publish' to publish to PyPI."

# Bump version and tag (patch, minor, or major)
version-bump level="patch":
    uv run bumpver update --{{level}} --commit --tag

# Publish to the private artifactory
publish:
    uv publish --publish-url https://artifactory.dummy.com/pypi
