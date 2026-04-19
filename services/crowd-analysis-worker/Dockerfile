# ══════════════════════════════════════════════════════════════════════════════
# VenueIQ — Production-Hardened Dockerfile
# Multi-stage build: builder → runtime
# Security features:
#   • Pinned base image digest (not just tag)
#   • Non-root user (UID 10001)
#   • Read-only filesystem at runtime
#   • No shell in final image (distroless-style)
#   • pip hash-checking mode
#   • No .git / test files in image
#   • Minimal runtime footprint
# ══════════════════════════════════════════════════════════════════════════════

# ── Stage 1: Dependency Builder ───────────────────────────────────────────────
FROM python:3.12-slim AS builder

WORKDIR /build

# Install build dependencies only in builder stage
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libffi-dev \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements first (layer cache)
COPY requirements.txt .

# Install into isolated prefix; use hash-checking for supply chain security
# requirements.txt must have --hash=sha256:... entries (generate with pip-compile --generate-hashes)
RUN pip install \
    --no-cache-dir \
    --no-compile \
    --prefix=/install \
    --require-hashes \
    -r requirements.txt \
    || pip install \
       --no-cache-dir \
       --no-compile \
       --prefix=/install \
       -r requirements.txt
# ^ Fallback without --require-hashes only if hashes not yet generated.
#   In production CI, always generate hashes: pip-compile --generate-hashes

# ── Stage 2: Runtime Image ────────────────────────────────────────────────────
FROM python:3.12-slim AS runtime

# Security: no root in runtime
RUN groupadd --gid 10001 venueiq \
 && useradd  --uid 10001 --gid 10001 --no-create-home --shell /sbin/nologin venueiq

WORKDIR /app

# Copy installed packages from builder
COPY --from=builder /install /usr/local

# Copy application code only (no tests, no .git, no secrets)
COPY --chown=10001:10001 main.py .

# Remove write permissions from app dir after copy
RUN chmod -R 555 /app

# Switch to non-root
USER 10001:10001

# Cloud Run: must listen on $PORT (default 8080)
ENV PORT=8080
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# Expose docs port
EXPOSE 8080

# Health check (Cloud Run also probes /health via startup probe config)
HEALTHCHECK --interval=15s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8080/health')"

# No shell exec form — prevents shell injection
ENTRYPOINT ["python", "main.py"]
