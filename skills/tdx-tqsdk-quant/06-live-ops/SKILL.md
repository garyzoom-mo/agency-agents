---
name: tdx-quant-06-live-ops
description: 通达信×TqSdk 量化工作流 阶段 6 —— 实盘运维。适用场景：信号桥已经跑通，要自动化每日运行、留审计痕迹、对账、设置停机开关、处理事故。含 Windows 任务计划/cron 配置、交易日判断、审计 JSON、对账表、风控红线自动执行、5 步事故处置 SOP、清空信号演练与连续三天验收。提炼自 DevOps Automator、SRE、Incident Response Commander、Secrets Engineer、Reality Checker。
---

# 阶段 6：实盘运维

> 目标：让这套东西**每天自己跑、出错会吵、坏事能停、事后能查**。
> 判据不是"今天跑成功了"，而是"连续三个交易日成功，且我演练过它失败的样子"。

## 输入 / 输出

- **输入**：阶段 4 的落盘脚本 + 阶段 5 的公式与验收记录
- **输出**：① 定时任务配置 ② 审计与日志规范 ③ 对账表 ④ 停机开关与事故 SOP ⑤ 三次连续成功记录

## 1. 调度（日频信号的推荐时序）

| 时间 | 动作 | 失败时会怎样 |
|---|---|---|
| 15:10 | 拉取当日数据、算信号、`--dry-run` 打印 | 无声，只写日志 |
| 15:30 | 正式写入通达信（`export` 模式） | 退出码非 0 → 告警；**不落盘好过落半截** |
| 15:35 | `verify` 只读校验（含"最新日期距今几天"） | 告警：数据过期或格式异常 |
| 次日 09:10 | 盘前人工确认一次（看图/看板块） | 人工决定是否交易 |

**Windows 任务计划**：

```bat
schtasks /Create /TN "TqQuant\Export" /SC DAILY /ST 15:30 ^
  /TR "cmd /c cd /d D:\q && C:\Python311\python.exe example_strategy.py export --tdx-dir C:\new_tdx >> D:\q\logs\export.log 2>&1"

schtasks /Create /TN "TqQuant\Verify" /SC DAILY /ST 15:35 ^
  /TR "cmd /c cd /d D:\q && C:\Python311\python.exe example_strategy.py verify --tdx-dir C:\new_tdx >> D:\q\logs\verify.log 2>&1"
```

**Linux / macOS cron**：

```cron
30 15 * * 1-5 cd /opt/q && /opt/q/.venv/bin/python example_strategy.py export --tdx-dir /mnt/tdx >> logs/export.log 2>&1
```

要点：
- 任务计划里必须**显式设置工作目录**并使用**绝对路径**（python.exe、脚本、通达信目录）。
- 凭据放系统环境变量（Windows 可用"系统属性 → 环境变量"或 `setx`），不要写进 `.bat` 文件。
- 日志**带日期滚动**（`logs/export_20260917.log`），别让单个文件无限增长。
- 非交易日也跑：让"没数据"变成一次正常退出（见下），而不是让任务计划静默失败。

## 2. 交易日判断（不许自己猜日历）

```python
# 首选：用数据本身判断 —— 今天有没有新 K 线产生
# （TqSdk 返回的最后一根已收盘K线日期 != 上次记录的日期 → 今天是交易日）
state = json.loads(Path("state/last_run.json").read_text(encoding="utf-8"))
incoming_day = last_closed_bar_day
if incoming_day == state.get("last_day"):
    print(f"[skip] {incoming_day} 已处理过，本次不做任何写入")
    return 0
```

**硬规则**：只用"数据里真实存在的交易日"。自己维护节假日表迟早会错，而且错了很难发现。

## 3. 审计留痕（每次运行一份 JSON）

```json
{
  "run_at": "2026-09-17T15:30:02",
  "mode": "export",
  "params": {"fast": 5, "slow": 20, "vol_max": 0.05},
  "symbols": 300,
  "bars_latest_day": 20260917,
  "rows_written": 300,
  "files": [
    {"path": "T0002/signals/signals_user_100/1_600519.dat", "sha256": "…", "rows": 121}
  ],
  "exit_code": 0,
  "duration_sec": 12.4
}
```

用途：三个月后有人问"9 月 17 号那天到底写了什么" → 直接看 JSON，不用猜。
`sha256` 还能证明"我确实写了一版，后来又被人/程序改过没有"。

同时保留最近 N 天的目录快照（zip 备份 `T0002/signals`），出问题时能一键回退：

```bash
python -c "import shutil,datetime as d; shutil.make_archive('backup/signals_'+d.date.today().strftime('%Y%m%d'),'zip','C:/new_tdx/T0002/signals')"
```

## 4. 对账（每周一次，别偷懒）

| 对账项 | 数据来源 | 判据 | 差异处理 |
|---|---|---|---|
| 信号 vs 实际持仓 | `signals_user_100` vs 券商持仓 | 方向一致 | 记录原因（未成交/涨跌停/手动干预） |
| 通达信显示 vs Python 输出 | 通达信截图 vs CSV | 逐日一致 | 回到阶段 5 排查 |
| 回测假设 vs 实盘成交 | 成交回报 vs 假设表 | 滑点 ≤ 假设 ×1.5 | 超出则暂停并重估成本 |
| 运行成功率 | 审计 JSON / 日志 | 近 20 个交易日 100% 成功 | 任何失败都要有结论（已修复 / 已知限制） |
| 数据新鲜度 | `verify` 输出 | 最新日期距今 ≤ 1 个交易日 | 超了立刻查管道 |

用 `templates.md` 的 **T9 实盘对账表**登记。

## 5. 停机开关与风控红线（必须能一分钟内停掉）

| 开关 | 实现 | 效果 |
|---|---|---|
| 暂停写入 | 项目根目录放一个 `PAUSE` 文件，脚本启动即检查并退出 0 | 停止产出新信号（旧信号保留） |
| 清空信号 | 用一个"全 0"的序列覆盖，或删除 `.dat`/`.blk` | 通达信回到空仓显示，策略"下线" |
| 切换到"只观察"数据号 | 把数据号改成 199（预留的观察位），公式改指过去 | 不干扰正在跑的实盘信号 |
| 彻底停用 | 删除定时任务：`schtasks /Delete /TN "TqQuant\Export" /F` | 不再运行 |

**风控红线自动执行**（写进脚本，不靠人记）：

```python
# 示例：触发红线时，信号强制置 0 并写一条外部数据说明原因
if account_drawdown > 0.20 or consecutive_losses >= 3:
    bridge.write_user_series(symbol, {day: 0.0 for day in trading_days})
    bridge.write_extern_user([(symbol, "红线停机：回撤超限", 0.0, DATA_ID_NOTE)])
```

**默认立场**：所有自动下单相关的开关默认关闭。人工确认后才交易（阶段 1 契约里的 `{{是否真下单}}`）。

## 6. 事故处置 SOP（5 步，照做即可）

1. **止血**（1 分钟内）：放 `PAUSE` 文件；必要时清空信号（见上表）；**不要**边查边继续写入。
2. **取证**：保存审计 JSON、当日日志、`T0002/signals` 快照、通达信截图。先存证再动手改。
3. **定位**：按这张表先怀疑最常见的三类原因 ——

   | 症状 | 首选怀疑对象 |
   |---|---|
   | 通达信显示全 0 | 数据号 / 目录不对 / 文件夹被占用 |
   | 数据没更新 | 定时任务没跑 / 跑了但 `--dry-run` / 脚本提前静默退出 |
   | 数值对不上 | 复权口径 / 交易日对齐 / 缓存未刷新 |
   | 板块里混入怪票 | `.blk` 市场号写错 |

4. **修复并验证**：修完先 `selftest` → `demo` → `dry-run` → 真实 `export` → `verify`，四关都过再恢复定时任务。
5. **复盘**：用 `templates.md` **T10** 写复盘，把"这次为什么没早点发现"写成一条可执行的检查项，
   并**写回本 skill 包**（对应阶段的常见坑/故障对照表里加一行）。这才是这个 skill 包会越用越好的原因。

## 7. 凭据与安全

- `TQ_USER/TQ_PASS` 只放系统环境变量或凭据管理器；日志与审计 JSON 里**不出现**密码。
- 定期轮换（建议每季度），轮换后立即验证一次 `export` 全流程。
- 共享机器上，`T0002` 目录权限收窄到当前用户；注意云同步盘（OneDrive/坚果云）会锁定文件导致写入失败。
- 任何"把账号密码写进脚本方便一点"的改动，一律拒绝。

## 8. 验收：连续三天 + 两次演练

| # | 判据 | 证据 | 通过？ |
|---|------|------|--------|
| 1 | 定时任务连续 3 个交易日成功（含 verify） | 3 份审计 JSON | ☐ |
| 2 | 演练"清空信号"：通达信确实回落到空仓/空板块状态 | 演练记录 + 截图 | ☐ |
| 3 | 演练"写失败"：文件被占用时脚本退出码非 0 且日志明确 | 演练记录 | ☐ |
| 4 | 停机开关（`PAUSE`）实测有效 | 演练记录 | ☐ |
| 5 | 对账表已填（至少一周） | T9 表 | ☐ |
| 6 | 备份可还原（真的解压还原过一次） | 还原记录 | ☐ |
| 7 | 凭据未出现在任何仓库文件与日志里 | 全仓 grep + 日志抽查 | ☐ |
| 8 | 已知限制写进运维手册（刷新行为、北交所口径、成本假设） | 手册 | ☐ |

**只有全部打勾才算"上线"**。任何一项缺失，状态就是 NEEDS WORK —— 这不是形式主义：
上面每一条都对应一次真实世界里亏过钱或错过大行情的场景。
