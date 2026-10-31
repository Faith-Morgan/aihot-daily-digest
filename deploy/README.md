# 独立运行：08:30 目标，08:25—08:35 完成验收

这里提供 Linux + systemd 的部署配置，供常在线服务器使用。调度与 Python 执行都在服务器上，不依赖 GitHub cron 或 Actions runner 排队。仅复制这些文件不代表已部署或达到时效要求。

## 前提

- 已有 Linux 常在线服务器，Python 3.10+、systemd、util-linux 的 flock；时间同步正常，08:30 时段能访问 AI HOT 和企业微信。
- 代码放在 `/opt/aihot-daily-digest`；普通运行账户为 `aihot`；持久状态在 `/var/lib/aihot-daily`。这些路径不能是随任务销毁的临时盘。
- `WECOM_WEBHOOK` 由管理员直接写入服务器 `/etc/aihot-daily.env`，文件只允许 root 读取。不要通过公开 PR、聊天或日志传递凭据。文件中只配置必要业务变量，勿设置 `AIHOT_PERSIST_GIT`。

管理员创建无登录权限的 `aihot` 账户，将代码安装为 root 所有、运行账户可读，并将状态目录交给 `aihot`。用当前默认分支的 `state/pushed_ids.json` 初始化独立状态目录；若存在 `state/delivery.json`，同时迁移它，保留未完成/待核对记录。不要用空状态覆盖既有文件。

## 切换前先阻止两个运行环境同时发送

1. 在 GitHub 仓库 Actions Variables 中设置 `DELIVERY_RUNNER=external`，并确认没有仍在进行的旧日报 job。新版本工作流会跳过发送 job。
2. 将**最终**状态复制到独立服务器。迁移期间保持服务器的两个 timer 停止；迁移后核对状态内容及 SHA256。若旧版本当天已经推送而没有 delivery 回执，应核对群内分片并确认去重记录已包含这些条目，再安排次日首个定时运行。
3. 只在一个运行环境启用推送。回滚到 Actions 前，先停独立 timer 和 service，并把最新的两个状态文件写回默认分支，然后再清除 `DELIVERY_RUNNER`。不能用旧的 Git 状态直接恢复发送。

## 配置与启动

环境文件（由管理员填入真实值）：

```ini
WECOM_WEBHOOK=填写服务器上的真实Webhook
PUSH_CHANNEL=wecom
```

完成账户、路径、环境文件和状态迁移后，安装本目录 `systemd/` 中的四个单元。以下操作由服务器管理员执行，不会由仓库自动远程执行：

```bash
sudo install -m 0644 deploy/systemd/*.service deploy/systemd/*.timer /etc/systemd/system/
sudo systemd-analyze verify /etc/systemd/system/aihot-*.service /etc/systemd/system/aihot-*.timer
sudo systemctl daemon-reload
sudo systemctl enable --now aihot-daily.timer aihot-check.timer
systemctl list-timers 'aihot-*'
```

timer 显式使用 UTC：00:30 = 北京/台北 08:30；00:36 检查当天完成回执。`AccuracySec=1s`、`RandomizedDelaySec=0` 限制本机人为抖动；`Persistent=true` 在宕机后检查错过的触发，但不把迟到启动记成准时。`AIHOT_ENFORCE_WINDOW=1` 阻止窗口外开始新的 POST；已发送请求的响应和群内显示时间仍需验证。

主服务通过 flock 串行运行，同一个服务同时只启动一份。仅 GET 瞬时故障耗尽会自动重启，最多 3 次启动；08:35 后即使重启也停止迟到补发。其他错误和送达结果不明不会自动重启。08:36 的检查会将缺失、未完成及超窗结果标红，并尽力经原 webhook 告警；若企微通道也故障，以 systemd 失败状态和日志为准。

本机检查不能检测服务器整机离线。要在整机故障时及时通知，需在所选运行平台另配存活监控/独立告警目标；不能据此配置声称高可用或承诺绝无延迟。

## 验收证据

1. 部署前执行 `python3 -m unittest discover -s tests -v`，确认离线回归通过。
2. 核对 `timedatectl status` 的时间同步、`systemctl list-timers 'aihot-*'` 的下次触发时间、两个 timer 的启用状态。
3. 在真实定时运行后查看 `journalctl -u aihot-daily.service -u aihot-check.service --since today`，确认 08:30 启动、全部片确认、状态落盘、检查成功。`systemctl is-failed aihot-daily.service aihot-check.service` 应均不处于失败状态。
4. `/var/lib/aihot-daily/delivery.json` 应有今日日期、分片 SHA256、`confirmed == len(chunks)`、`inflight == null`、08:25—08:35 的 `completed_at`；无新增条目时分片为 0，须据日志如实区分。
5. 在目标群核对全部分片的数量、内容和实际出现时间。只有接口确认和本机完成时间，不能证明群内显示时间。至少跨过一次真实 08:30 定时触发后才能称完成部署验证；连续准时能力需要多日记录。

## 人工处理送达不明

先停止 timer，确保没有发送 service 正在运行，按 `delivery.json` 的 `inflight` 和固定内容在目标群核对。只有确认后才执行下列其一；无需向这两个状态命令提供 webhook：

```bash
sudo -u aihot env AIHOT_STATE_DIR=/var/lib/aihot-daily python3 /opt/aihot-daily-digest/scripts/aihot_daily.py --resolve-inflight sent
# 或：确认未收到时，将最后的 sent 换成 retry
```

该命令只保存确认，不自动发送。之后恢复 timer；若已过当天允许窗口，保留记录并排查当天失约原因，不以迟到重发冒充准时成功。不要直接编辑确认计数、删除状态、强行忽略损坏校验，也不要同时从个人电脑裸跑脚本。

## 数据覆盖边界

脚本保持原来的滚动 24 小时、最多 50 条精选口径，没有历史全量新闻抓取承诺。恢复时沿用被冻结的日报，避免上游变化导致分片移动。企微未提供本项目可用的幂等键，网络响应不明时需要人工核对，不能同时承诺自动重发和绝不重复。

参考：[systemd timer 官方文档](https://www.freedesktop.org/software/systemd/man/latest/systemd.timer.html)、[GitHub schedule 规则](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule)。
