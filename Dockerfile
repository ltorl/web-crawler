# Full-featured deploy (includes a real Chromium so "Verify with JavaScript"
# works). Built on the official Playwright image which already ships the
# browser + all system libraries.
#
# On Render: New → Web Service → pick this repo → Runtime: Docker.
FROM mcr.microsoft.com/playwright/python:v1.48.0-jammy

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && playwright install chromium

COPY . .

# Render injects $PORT. SSE streams are long-lived → no worker timeout.
CMD gunicorn app:app --bind 0.0.0.0:${PORT:-10000} --workers 1 --threads 8 --timeout 0
