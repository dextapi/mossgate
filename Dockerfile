FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PORT=8787 HOST=0.0.0.0 KEY_FILE=/data/key.env
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY run.py models.json ./
RUN useradd -r -u 10001 mossgate && mkdir -p /data && chown -R mossgate /app /data
USER mossgate

EXPOSE 8787
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
  CMD python -c 'import urllib.request,os;urllib.request.urlopen("http://127.0.0.1:%s/_health"%os.environ["PORT"],timeout=4)'

CMD ["python", "run.py"]
