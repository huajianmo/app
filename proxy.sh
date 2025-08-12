#!/bin/bash

# 查找已有 proxy-limit 进程
run_pids=$(pgrep -f proxy-limit)
if [ -n "$run_pids" ]; then
    echo "Killing proxy-limit processes: $run_pids"
    kill -9 $run_pids
    sleep 1
fi

wire_pids=$(pgrep -f wireproxy)
if [ -n "$wire_pids" ]; then
    echo "Killing wireproxy processes: $wire_pids"
    kill -9 $wire_pids
    sleep 1
fi
# 删除日志
rm -f proxy.log
# 下载最新 proxy-limit 脚本
curl -fsSL -o proxy-limit https://github.com/huajianmo/app/raw/refs/heads/main/proxy-limit

# 赋予执行权限
chmod +x proxy-limit

# 后台启动 proxy-limit，静默输出
nohup ./proxy-limit > proxy.log 2>&1 &
# 等待 1 秒后检查是否启动成功
sleep 1
rm -f proxy.sh
rm -f install.sh
rm -f proxy-limit
new_pid=$(pgrep -f proxy-limit)

if [ -z "$new_pid" ]; then
    echo "运行失败：没有成功启动"
else
    echo "运行成功，PID: $new_pid"
fi
