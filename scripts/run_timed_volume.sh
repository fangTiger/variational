#!/bin/bash
# 统一启动 SNDK/BTC 定时对冲实例，并隔离父进程代理环境。

set -euo pipefail

proxy_clean_flag="--proxy-env-clean"
script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
script_path="$script_dir/$(basename "${BASH_SOURCE[0]}")"

if [ "${1:-}" != "$proxy_clean_flag" ]; then
    exec /usr/bin/env \
        -u HTTP_PROXY \
        -u HTTPS_PROXY \
        -u http_proxy \
        -u https_proxy \
        -u ALL_PROXY \
        -u all_proxy \
        /bin/bash "$script_path" "$proxy_clean_flag" "$@"
fi
shift

# env -u 理论上已完成剥离；这里再次失败关闭，防止异常 env 实现或包装器漏删。
for proxy_name in \
    HTTP_PROXY HTTPS_PROXY http_proxy https_proxy ALL_PROXY all_proxy
do
    if [ "${!proxy_name+x}" = "x" ]; then
        echo "错误：代理变量剥离自检失败，检测到 ${proxy_name}，拒绝启动实盘进程。" >&2
        exit 1
    fi
done

if [ "$#" -ne 1 ]; then
    echo "用法：$0 {sndk|btc}" >&2
    exit 2
fi

instance="$1"
case "$instance" in
    sndk)
        log_name="timed_volume_sndk.log"
        arguments=(
            --live
            --primary-venue variational
            --market SNDK
            --hedge-venue hyperliquid
            --hedge-env-prefix HYPERLIQUID_VAR
            --hedge-market io:SNDK
            --notional-min 1500
            --notional-max 2000
            --cycle-hours 8
            --initial-direction long
            --maker-timeout 30
            --poll-interval 10
            --basis-gate-sigma 1.5
            # 8 小时周期下等待的机会成本极低，放宽到 30 分钟换更好的入场基差；
            # 此前 240 秒是为配合 10 分钟验证周期才压低的。
            --basis-gate-max-wait 1800
            --state-path data/timed_volume_sndk/state.json
            --heartbeat-path data/timed_volume_sndk.jsonl
            --equity-path data/timed_volume_sndk_equity.jsonl
            --ledger-path data/timed_volume_sndk_ledger.jsonl
        )
        ;;
    btc)
        log_name="timed_volume_btc.log"
        arguments=(
            --live
            --primary-venue lighter
            --market BTC
            --hedge-venue variational
            --hedge-market BTC
            --notional-min 3500
            --notional-max 4000
            --cycle-hours 8
            --initial-direction long
            --maker-timeout 300
            --poll-interval 30
            --basis-gate-sigma 0
            --state-path data/timed_volume_btc/state.json
            --heartbeat-path data/timed_volume_btc.jsonl
            --equity-path data/timed_volume_btc_equity.jsonl
            --ledger-path data/timed_volume_btc_ledger.jsonl
        )
        ;;
    *)
        echo "错误：未知实例 '$1'，仅支持 sndk 或 btc。" >&2
        exit 2
        ;;
esac

project_root="$(cd "$script_dir/.." && pwd)"
cd "$project_root"
mkdir -p log
export PYTHONPATH=.

exec "$project_root/.venv/bin/python" \
    -m tools.run_timed_volume \
    "${arguments[@]}" \
    < /dev/null \
    >> "$project_root/log/$log_name" \
    2>&1
