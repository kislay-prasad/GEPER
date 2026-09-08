# =============================================================================
# clinical/Dockerfile -- THE CLINICAL SERVICE IMAGE
# =============================================================================
# A SEPARATE FILE, AND THAT IS THE DECISION THIS HEADER EXISTS TO EXPLAIN.
#
# The obvious move was to give the root Dockerfile an ENTRYPOINT. It already
# ends in `CMD ["/bin/bash"]`, which reads like an oversight -- and it is not.
# The comment directly above it states the reason: that image "supports three
# independently valid, already-documented ways to run this pipeline ... and
# hard-coding one as THE entrypoint would silently make the other two
# second-class." GEPER standalone, Kim standalone, and the combined bridge
# workflow. That is a decision somebody made and wrote down, not a
# placeholder somebody forgot -- the opposite of `_get_data_access`'s
# "cache as singleton", which read like a decision and was a trap.
#
# So the clinical service is a FOURTH way to run this tree, and taking the
# default for it would demote the three that are documented.
#
# Nor can it be a new stage appended to the root Dockerfile: `docker build`
# and `docker-compose.yml` (which passes no `target:`) both default to the
# LAST stage, so appending one silently changes what everybody's build
# produces. A stage that must never accidentally become the default does not
# belong in a file whose default is positional.
#
# A HEALTHCHECK is the second reason. In the shared image it would run in the
# three non-service modes too, where nothing listens on a port, and mark
# perfectly healthy pipeline containers unhealthy. A healthcheck belongs to
# something that serves.
#
# BUILD:
#   docker build -f clinical/Dockerfile -t geper-clinical:latest \
#     --build-arg BASE_IMAGE=geper:latest .
# RUN (credentials via --env-file, never on the command line):
#   docker run --rm -p 8000:8000 --env-file .env geper-clinical:latest
# The .env file supplies CLINICAL_DSN.

ARG BASE_IMAGE=geper:latest
FROM ${BASE_IMAGE}

# The base image installs geper/requirements.txt, which already carries
# fastapi, uvicorn[standard] and psycopg[binary]. This line exists for what it
# does NOT already have -- bcrypt, which clinical/data_access.py imports
# lazily inside BcryptHasher and which was declared in no requirements file in
# the repository until clinical/requirements.txt. Installing the whole file
# rather than the one missing package keeps a single declaration of what this
# service needs; pip skips what is already satisfied.
COPY clinical/requirements.txt /tmp/clinical-requirements.txt
RUN pip install --no-cache-dir -r /tmp/clinical-requirements.txt

WORKDIR /app

# THE SOURCE IS COPIED IN, NOT INHERITED, AND THIS WAS A REAL FAILURE BEFORE IT
# WAS A COMMENT. The base image bakes its own `COPY . /app`, so an older base
# carries an older tree: the first run of this image died with
# `Error loading ASGI app. Could not import module "clinical.app"` because the
# base predated that module. Inheriting source from a base image makes the
# service's behaviour depend on when the base was built, which is exactly the
# kind of thing that is invisible until it is a production incident.
COPY clinical /app/clinical

# Documentation, not a port binding -- `docker run -p` still decides what is
# published. Recorded so `docker inspect` answers the question.
EXPOSE 8000

# CLINICAL_DSN is deliberately NOT given a default. A default would let the
# service start pointed at something nobody chose; unset, /readyz answers 503
# "dsn_not_configured" and says so by name.
ENV CLINICAL_HOST=0.0.0.0 \
    CLINICAL_PORT=8000

# PYTHON, NOT curl -- AND THIS WAS CHECKED RATHER THAN ASSUMED. `command -v
# curl` in the base image returns NOTHING: curl is not installed. A curl-based
# HEALTHCHECK would have exited non-zero every single time and marked a
# perfectly working service unhealthy, with nothing wrong in its logs -- a
# false RED, the mirror of the false green this endpoint exists to prevent.
# Python is guaranteed present: it is what runs the service.
#
# IT PROBES /readyz, NOT /healthz, AND THAT IS THE POINT OF THE ENDPOINT: a
# service that boots and returns 500 on every request passes any liveness
# probe. Before F1 this service would have done exactly that -- both
# dependencies raised NotImplementedError, so the process was up and every
# real request was a 500. /readyz opens a connection and runs SQL, so an
# unreachable or unbootstrapped database reports unhealthy instead of ready.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import sys,urllib.request; r=urllib.request.urlopen('http://127.0.0.1:8000/readyz', timeout=4); sys.exit(0 if r.status==200 else 1)"]

# ENTRYPOINT plus an overridable CMD: the container always runs the clinical
# service, and the argument list stays editable (`--workers`, a different
# port) without an --entrypoint override.
ENTRYPOINT ["python", "-m", "uvicorn"]
CMD ["clinical.app:app", "--host", "0.0.0.0", "--port", "8000"]
