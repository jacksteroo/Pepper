FROM python:3.12-slim

WORKDIR /app

ENV PYTHONUNBUFFERED=1

# System deps for psycopg2, sentence-transformers
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies
COPY pyproject.toml .
RUN pip install --no-cache-dir -e ".[dev]"

# Copy source
COPY agent/ agent/
COPY agents/ agents/
COPY subsystems/ subsystems/
COPY docs/ docs/

CMD ["python", "-m", "agent.start"]
