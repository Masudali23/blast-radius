# Same base image (by digest) the commits were labelled in: its Python and SQLite decide
# which sqlglot tests run and how.
FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90

ENV PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    SKIP_INTEGRATION=0

# The labelling environment's packages, from PyPI.
COPY requirements.lock /app/requirements.lock
RUN pip install -r /app/requirements.lock

# sqlglot at the pinned commit, straight from its own repository.
COPY tools/fetch_sqlglot.py /app/tools/fetch_sqlglot.py
RUN python /app/tools/fetch_sqlglot.py /app/repo

COPY br /app/br
COPY selector.py /app/selector.py
COPY maps /app/maps

# Byte-compile once: the run-time disk is read-only.
RUN python -m compileall -q /app/repo/sqlglot /app/repo/tests /app/repo/sqlglotc /app/br /app/selector.py \
    && chmod -R a+rX /app

WORKDIR /app
CMD ["python", "/app/selector.py"]
