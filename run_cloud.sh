#!/usr/bin/env bash
# Start Job Finder on a cloud host (Render, Railway, a VM, Docker…).
#   * listens on 0.0.0.0:$PORT (the host sets PORT; default 8501)
#   * needs APP_PASSWORD set as an environment variable or Streamlit secret - the app stays locked without one
# Hugging Face Spaces (Streamlit SDK) and Streamlit Community Cloud start the app themselves; you don't need this there.
set -euo pipefail
cd "$(dirname "$0")"
STREAMLIT=${STREAMLIT:-streamlit}
[ -x .venv/bin/streamlit ] && STREAMLIT=.venv/bin/streamlit
exec "$STREAMLIT" run app.py --server.port="${PORT:-8501}" --server.address=0.0.0.0 --server.headless true \
  --browser.gatherUsageStats false
