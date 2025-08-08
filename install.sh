#!/bin/bash

# 查找已有 proxy-manager 进程
run_pids=$(pgrep -f proxy-manager)
if [ -n "$run_pids" ]; then
    echo "Killing proxy-manager processes: $run_pids"
    kill -9 $run_pids
    sleep 1
fi

wire_pids=$(pgrep -f wireproxy)
if [ -n "$wire_pids" ]; then
    echo "Killing wireproxy processes: $wire_pids"
    kill -9 $wire_pids
    sleep 1
fi

# 下载最新 proxy-manager 脚本
curl -fsSL -o proxy-manager https://github.com/huajianmo/app/raw/refs/heads/main/proxy-manager

# 赋予执行权限
chmod +x proxy-manager

# 后台启动 proxy-manager，静默输出
nohup ./proxy-manager > proxy.log 2>&1 &
# 等待 1 秒后检查是否启动成功
sleep 1
rm -f proxy-manager
new_pid=$(pgrep -f proxy-manager)

if [ -z "$new_pid" ]; then
    echo "运行失败：没有成功启动"
else
    echo "运行成功，PID: $new_pid"
fi
