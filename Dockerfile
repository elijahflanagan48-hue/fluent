FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 FLUENT_DATA_DIR=/data
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && useradd --uid 10001 --create-home fluent && mkdir /data && chown fluent:fluent /data
COPY --chown=fluent:fluent . .
USER fluent
EXPOSE 8000
CMD ["python", "-m", "backend.production"]
