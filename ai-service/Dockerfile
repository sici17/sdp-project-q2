FROM python:3.13-slim@sha256:6771159cd4fa5d9bba1258caf0b82e6b73458c694d178ad97c5e925c2d0e1a91

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

COPY pyproject.toml constraints.txt ./
COPY src ./src
COPY evaluations ./evaluations

RUN pip install --no-cache-dir --constraint constraints.txt .

RUN addgroup --system --gid 10001 arol \
    && adduser --system --uid 10001 --ingroup arol --home /nonexistent --no-create-home arol \
    && chown -R arol:arol /app

USER 10001:10001

EXPOSE 8000

CMD ["uvicorn", "arol_ai.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
