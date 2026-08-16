"""live-signal 的常驻后台任务：WSS 逐笔订阅、K 线轮询、每日历史快照回填。

都是 `main.py` 的 FastAPI `lifespan` 启动的 asyncio 任务，跟请求处理共用同一个事件循环、
同一个进程——不是三个独立的 cron 触发的短命 CLI 进程，见根目录 README 的部署说明。

阶段 0 明确不做进程级"保活"（崩溃自动重启）——这个进程本身的存活由外部工具负责
（比如 systemd/launchd，或者干脆手动重启），不在这几个任务的代码范围内。以后如果要跑
多个 `live-signal` 实例（比如横向扩容），这几个任务（尤其是 WSS 订阅）需要引入分布式锁
（如 Postgres advisory lock）保证只有一个实例真正执行，不是本次要解决的问题。
"""
