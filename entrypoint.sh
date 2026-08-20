#!/bin/sh
# 容器入口: 自愈数据目录权限后降权运行
set -e

# /data 为挂载卷时宿主机目录属主可能不是 appuser (uid 10001),
# 首次部署常见; 以 appuser 身份检测可写性, 不可写则由 root 修正
if ! su-exec 10001:10001 sh -c 'touch /data/.write_test 2>/dev/null'; then
    echo "[entrypoint] /data 不可写, 修正属主为 appuser"
    chown -R 10001:10001 /data
fi
rm -f /data/.write_test 2>/dev/null || true

# 降权运行
exec su-exec 10001:10001 python -m app "$@"
