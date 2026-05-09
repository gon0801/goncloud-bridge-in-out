# Stage 1: install dependencies into an isolated venv
FROM python:3.11.12-slim AS builder
WORKDIR /build
RUN python -m venv /venv
COPY requirements.txt .
RUN /venv/bin/pip install --no-cache-dir -r requirements.txt

# Stage 2: runtime — only explicitly listed files reach this image.
# Nothing from the build context that isn't named below can end up in a layer,
# so secrets (.env*, tokens, .meli_tokens.json) are structurally excluded
# even if .dockerignore is ever misconfigured (D6.8).
FROM python:3.11.12-slim
COPY --from=builder /venv /venv
ENV PATH="/venv/bin:$PATH"
WORKDIR /app
COPY *.py /app/
COPY *.html /app/
COPY static/ /app/static/
ENV PYTHONUNBUFFERED=1
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8099"]
