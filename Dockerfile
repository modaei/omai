FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Install only packaged runtime dependencies. Development tools and local
# virtual environments are intentionally excluded from the public demo image.
COPY pyproject.toml README.md ./
COPY src ./src
# Capability guidance and schema documentation are loaded from the repository
# root by Omai's domain helpers, so they must be present in the runtime image.
COPY knowledge ./knowledge
RUN pip install --upgrade pip && pip install .

RUN useradd --create-home --shell /usr/sbin/nologin omai
USER omai

EXPOSE 50009

CMD ["uvicorn", "omai.api.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "50009"]
