# DRISHTI API service.
#
# This is a LONG-RUNNING container by necessity, not preference. The Tier 2
# veto countdowns, the pending-alert registry, the ward buffer and the
# /ws/alerts WebSocket all live in process memory. On a platform that freezes
# the process after each response, the dead-man's switch silently stops
# firing — see docs/DEPLOYMENT.md.

FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# Dependencies first so a code change doesn't reinstall xgboost every build.
COPY backend/requirements.txt /app/backend/requirements.txt
RUN pip install --no-cache-dir -r /app/backend/requirements.txt

# The backend reads the model artifacts from ../ml/artifacts (see config.py),
# so both directories have to be in the image.
COPY backend/ /app/backend/
COPY ml/ /app/ml/

WORKDIR /app/backend

# Render and most PaaS providers inject $PORT. Default to 8000 locally.
ENV PORT=8000
EXPOSE 8000

# One worker on purpose. The in-memory alert registry is per-process, so a
# second worker would hold veto countdowns the first one can't see and
# /alerts/active would return different answers depending on which worker
# served the request. Scaling out needs Redis behind the dispatcher first.
CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT} --workers 1"]
