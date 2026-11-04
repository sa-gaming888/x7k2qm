# syntax=docker/dockerfile:1
# 程式映像：FROM 基底映像（Dockerfile.base，base.yml 建），只放程式與資料。
# 程式只用標準函式庫（DES 走內建純 Python 版），不 pip install 任何套件。
# 本機建：docker build --build-arg BASE=<基底映像> -t x7k2qm:local .

ARG BASE=ghcr.io/sa-gaming888/x7k2qm-base:latest
FROM ${BASE}
COPY --chown=65532:65532 . /app
RUN ["python", "-m", "compileall", "-q", "/app/x7k2qm"]
VOLUME ["/data"]
EXPOSE 8790
HEALTHCHECK --interval=60s --timeout=5s --start-period=180s --retries=3 \
  CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8790/health', timeout=4).status == 200 else 1)"]
ENTRYPOINT ["python", "-X", "utf8", "-m", "x7k2qm.autoplay.container"]
