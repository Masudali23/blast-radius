# Same base image (by digest) the commits were labelled in: its Python and SQLite decide
# which sqlglot tests run and how.
FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90

ENV PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    SKIP_INTEGRATION=0 \
    BR_CLEAN_STOP=0.4 \
    BR_CLEAN_MIN=0.4 \
    BR_CLEAN_FRAC=0 \
    BR_CLEAN_ANY=1 \
    BR_FIRST_ONLY=1 \
    BR_FIRST_N=2 \
    BR_LOOK_FIRST=1 \
    BR_P_PASS_REST=0.7

# The labelling environment's packages, from PyPI.
COPY requirements.lock /app/requirements.lock
RUN pip install -r /app/requirements.lock

# sqlglot at the pinned commit, straight from its own repository.
COPY tools/fetch_sqlglot.py /app/tools/fetch_sqlglot.py
RUN python /app/tools/fetch_sqlglot.py /app/repo

COPY br /app/br
COPY selector.py /app/selector.py
COPY maps /app/maps

# The executor tests' fixture tables, parsed once from the pinned checkout (br/tables.py).
# Optional: without the file the tests' own set-up parses them at run time.
# maps/frames.pkl holds what DuckDB answers to those tests' queries; it was written by this
# same script in this same image (.github/workflows/stores.yml), so the build need not ask
# DuckDB again.  Without it the script asks.
COPY tools/build_tables.py /app/tools/build_tables.py
RUN timeout 300 python /app/tools/build_tables.py /app/repo /app/tables.pkl /app/maps/frames.pkl || true

# What DuckDB answers to the optimizer tests' queries at the pinned commit (br/answers.py):
# maps/answers.pkl, written by tools/build_answers.py in this same image; built here if the
# file is not there.  Optional: without it every query goes to DuckDB.
COPY tools/build_answers.py /app/tools/build_answers.py
RUN if [ -f /app/maps/answers.pkl ]; then cp /app/maps/answers.pkl /app/answers.pkl; \
    else timeout 240 python /app/tools/build_answers.py /app/repo /app/answers.pkl || true; fi

# Byte-compile once: the run-time disk is read-only.  (The base image ships the standard
# library without bytecode; its directory holds the installed packages as well.)
RUN python -m compileall -q /app/repo/sqlglot /app/repo/tests /app/repo/sqlglotc /app/br /app/selector.py \
    && (python -m compileall -q -j 4 "$(python -c 'import sysconfig; print(sysconfig.get_paths()["stdlib"])')" || true) \
    && chmod -R a+rX /app

WORKDIR /app
CMD ["python", "/app/selector.py"]
