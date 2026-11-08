#!/usr/bin/env sh
# 本地 CVE 掃描（不在 workflow 做，使用者 2026-11-04）：Grype 列出所有等級的弱點，有就結束碼 1。
#   ./scan.sh                 # 本機建基底＋程式映像再掃
#   ./scan.sh <映像>          # 掃指定映像，例 ghcr.io/sa-gaming888/x7k2qm-base:latest
# 需要 docker；Grype 用官方映像跑，不用另外安裝。
set -eu
if [ $# -ge 1 ]; then
  IMAGES="$1"
else
  docker build -f Dockerfile.base -t x7k2qm-base:local .
  docker build --build-arg BASE=x7k2qm-base:local -t x7k2qm:local .
  docker run --rm x7k2qm:local --check
  IMAGES="x7k2qm-base:local x7k2qm:local"
fi
rc=0
for img in $IMAGES; do
  echo "== 掃描 $img"
  docker run --rm -v /var/run/docker.sock:/var/run/docker.sock anchore/grype:v0.97.1 \
    "$img" --fail-on negligible || rc=1
done
exit $rc
