FROM python:3.12-slim

WORKDIR /app

# Install dependencies (root requirements.txt mirrors backend/requirements.txt
# and includes Flask-Limiter which the app needs at import time). Using the
# root file here avoids a build failure if backend/ hasn't been pushed to
# the repo yet.
COPY requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of the project
COPY . .

# Railway injects $PORT at runtime; default to 5000 for local testing
ENV PORT=5000
EXPOSE 5000

CMD ["sh", "-c", "gunicorn --bind 0.0.0.0:${PORT} --workers 2 --timeout 60 wsgi:app"]
