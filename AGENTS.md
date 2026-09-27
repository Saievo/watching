# AGENTS.md

## 股票看板服务：一律后台起

`stock_web/server.py`（默认端口 8787）无论首次启动还是重启，都用**后台起**的方式：脱离当前 shell 会话、自成进程组，日志追加到 `stock_web/server.out`。

```bash
python3 - <<'PY'
import subprocess, os
cwd = "/Users/edward/stock/stock_web"
log = open(os.path.join(cwd, "server.out"), "ab")
p = subprocess.Popen(["python3", "server.py"], cwd=cwd, stdout=log,
                     stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                     start_new_session=True)
print("pid:", p.pid)
PY
```

关键是 `start_new_session=True`：`stock_web/start.sh` 和 `nohup … &` 都不满足这条，进程生命周期仍跟着父会话走。

重启顺序：kill 旧进程 → 等 8787 端口释放 → 后台起 → 验证。

**起好了的标准（三条全过）**

1. `ps -p <pid> -o pid,ppid,pgid` 显示 PPID 为 1、PGID 等于自身 PID
2. `lsof -nP -iTCP:8787 -sTCP:LISTEN` 有输出
3. `curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:8787/` 返回 200

**为什么**

2026-09-10 21:56，看板连同正在跑的 10 个分析子进程在同一毫秒集体消失——它当时挂在 Codex 应用下面，应用一退出整棵树被带走。

**重启的代价很小**

服务启动时会把残留的 `running` 任务标成中断并自动续跑（断点续跑、沿用同一报告目录），已完成的进度不会白跑。
