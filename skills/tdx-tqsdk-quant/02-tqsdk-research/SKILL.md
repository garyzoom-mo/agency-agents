---
name: tdx-quant-02-tqsdk-research
description: 通达信×TqSdk 量化工作流 阶段 2 —— TqSdk 研究环境与代码骨架。适用场景：开始用 TqSdk 取数据、算指标、写策略代码，需要一套参数外置、可离线自检、幂等、日志清晰、能安全空跑的工程骨架。含 TqSdk 3.10.x 已实测的 API 事实、股票/期货代码格式、无网络测试策略。提炼自 Data Engineer、Minimal Change Engineer、Test Automation Engineer、Code Reviewer。
---

# 阶段 2：TqSdk 研究环境与代码骨架

> 目标：产出一份**能在没有凭据、没有网络、没有通达信的环境里空跑通过**的策略代码。
> 空跑通过 = 逻辑通路正确；这一步通过之前，任何"回测结果"都不可信。

## 输入 / 输出

- **输入**：阶段 1 的规格书 + 参数表 + 标的池
- **输出**：① 可运行工程骨架 ② 离线自检模式 ③ 单元测试 ④ 数据获取脚本

## 1. 环境与依赖

```bash
# Python 3.8+（TqSdk 3.10.x 实测于 3.11）
python -m venv .venv && . .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install tqsdk pandas                          # 桥接模块本身零依赖，不需要 pandas
pip freeze > requirements.lock.txt                # 锁定版本，回测结论才可复现
```

**凭据**：`TQ_USER` / `TQ_PASS`（快期账号）只从环境变量读取，**永不写进代码或提交进仓库**。

```
# .gitignore
.env
*.lock.local
reports/
logs/
```

```python
# 正确做法
import os
user, password = os.getenv("TQ_USER"), os.getenv("TQ_PASS")
if not user or not password:
    raise SystemExit("缺少环境变量 TQ_USER / TQ_PASS")
```

## 2. TqSdk API 事实（3.10.2 实测，避免照抄过时博客）

| 用途 | 正确写法 | 关键注意 |
|---|---|---|
| 实时/序列 K 线 | `api.get_kline_serial(symbol, duration_seconds, data_length=200, adj_type=None)` | 日线用 `86400`；`adj_type="F"/"B"` 只对股票/基金有效；**多合约不支持 adj_type** |
| 历史区间 K 线（研究用） | `api.get_kline_data_series(symbol, duration_seconds, start_dt, end_dt, adj_type="F")` | 有缓存，适合批量下载；不推进回测时钟 |
| 合约行情 | `api.get_quote(symbol)` | `volume_multiple` 等字段在 quote 上 |
| 事件驱动 | `api.wait_update()` + `api.is_changing(klines.iloc[-1], "datetime")` | 新 K 线生成时所有字段都算变化 |
| 序列就绪判断 | `api.is_serial_ready(klines)` | 首次循环里数据可能是 NaN |
| 回测 | `TqApi(account=TqSim()/TqSimStock(), backtest=TqBacktest(start_dt, end_dt), auth=...)` | 回测**强制**模拟账户 |
| 回测统计 | `sim.tqsdk_stat`（**在账户对象上，不在 TqApi 上**） | 键：`ror/annual_yield/max_drawdown/sharpe_ratio/sortino_ratio/winning_rate/profit_loss_ratio/commission/tqsdk_punchline` |
| 手续费覆盖 | `TqSim.set_commission(symbol, 每手手续费)` / `get_commission` | **期货模拟账户有；股票 TqSimStock 3.10.x 没有此接口** |
| 时间转换 | `from tqsdk.tafunc import time_to_datetime` | `klines.datetime` 是**纳秒**时间戳 |
| 技术指标 | `from tqsdk.ta import MA, MACD, ...` | 指标与自研计算混用时，务必核对口径 |

**代码格式**：股票 `SSE.601456` / `SZE.000001` / `BSE.430047`；期货 `SHFE.rb2510`、`DCE.i2601`、`CZCE.OI001`、`CFFEX.IF2512`。
**账户**：期货 `TqSim` / `TqKq` / `TqAccount`；股票 `TqSimStock` / `TqKqStock`。股票下单 `volume` 单位是**股**（一手=100 股），期货是**手**。

## 3. 工程骨架（照抄 `reference/example_strategy.py` 的结构）

```
config.py 或命令行参数     ← 参数外置：代码里不出现裸数字（除数学常量）
data.py                    ← 只负责取数，返回 [(YYYYMMDD, 值)] 这类中立结构
strategy.py                ← 纯函数：bars → {日期: 信号}，无 IO、无全局状态
export.py                  ← 只负责落盘（tdx_bridge）
main.py                    ← 编排 + 参数解析 + 退出码
```

**纯函数化是这套骨架的核心**：`compute_position_signals(bars) -> {日期: 信号}` 不碰网络、不碰磁盘，
才能被单测穷举、被未来函数自检反复调用（阶段 3 依赖它）。

### 必须实现的四种模式（缺一不可）

| 模式 | 作用 | 硬要求 |
|---|---|---|
| `selftest` | 逻辑自检 + 未来函数前缀稳定性 | 不联网、不写文件 |
| `dry-run` | 打印将要写入的内容 | **不校验、不创建、不写入**目标目录（目标目录可以根本不存在） |
| `export` / `live` | 真跑 | 失败退出码非 0，且不留下半截数据 |
| `verify` | 只读回读校验 | 只读模式，能发现"文件在但数据是旧的" |

### 失败与日志规范

```python
# 退出码语义（定时任务靠它判断成败）
# 0 = 成功　1 = 有告警（如数据过期）　2 = 参数/凭据/环境错误　其他 = 未预期异常（直接抛出）
print(f"[{time.strftime('%H:%M:%S')}] {symbol}: 写入 {count} 条，最新 {last_day}", flush=True)
```

- 日志必须包含：时间、标的、条数、最新日期、仓位值。缺任何一项，出问题时都不好查。
- **失败即沉默是最大风险**：宁可红着退出，也不要"什么都没写还返回 0"。

## 4. 数据处理纪律

| 纪律 | 做法 | 原因 |
|---|---|---|
| 不用未完成 K 线 | 决策用 `klines.iloc[:-1]` 或等 `is_changing(..., "datetime")` | `iloc[-1]` 是**正在跳动**的那根 |
| 时间统一为 YYYYMMDD | `int(time_to_datetime(ns).strftime("%Y%m%d"))` | 通达信自定义数据只认这个格式 |
| 只用交易日 | 别自己造日历；用数据里真实存在的日期 | 停牌/节假日会造出幽灵信号 |
| 复权口径写清楚 | 股票研究统一 `adj_type="F"` 并记录 | 前复权/不复权混用，价格类信号会错位 |
| 缺失值显式处理 | NaN 直接跳过并计数，别让 NaN 顺着算下去 | 隐式传播会污染全部信号 |
| 数值精度 | 序列数据是 **float32**（约 7 位十进制），价格/信号足够，别往里塞大整数 ID | 二进制桥的物理限制 |

## 5. 测试策略（无网络、确定性）

```python
# 三条铁律：
# 1) 合成数据必须确定性 —— 同一个 symbol 永远得到同一组数据（用 seed 而不是随机）
# 2) 不 sleep、不等网络；所有测试都能在 CI 里 1 秒内跑完
# 3) 测试隔离 —— 每个用例一个临时目录，互相不污染
```

要覆盖的最小集合（`reference/test_example_strategy.py` 可作为起点）：
- 信号值域合法（只有 0/1 或 -1/0/1）、日期升序、不出现非交易日
- 历史不足时返回空而不是乱算
- 参数变化方向合理（如收紧波动率上限 → 开仓次数不增加）
- `dry-run` 真的不碰磁盘
- 未来函数自检能抓到故意作弊的实现（反向测试！）

## 6. 质量门

| # | 判据 | 证据 | 通过？ |
|---|------|------|--------|
| 1 | `selftest` 在无网络/无凭据环境通过 | 命令输出 | ☐ |
| 2 | `dry-run` 不创建、不修改任何文件 | 目标目录对比 | ☐ |
| 3 | 单元测试全绿且总耗时 < 5 秒 | 测试输出 | ☐ |
| 4 | 参数全部外置，代码里没有硬编码标的/阈值 | 代码走查 | ☐ |
| 5 | 凭据只从环境变量读，`.gitignore` 覆盖 | 代码 + git status | ☐ |
| 6 | 依赖版本已锁定 | `requirements.lock.txt` | ☐ |

## 7. 常见坑

1. **照抄博客里的 `acc.tqsdk_stat`**：在 3.10.x 里它挂在**账户对象**上（`sim.tqsdk_stat`），不是 `TqApi`。
2. **`api.set_commission()`**：该方法在新版 TqSdk 中已不存在；改为 `TqSim().set_commission(...)`，且股票模拟账户没有它。
3. **`iloc[-1]` 当已收盘价用**：回测里会导致"用未来数据"，实盘里会导致"信号来回跳"。
4. **多合约 + `adj_type`**：会直接报参数错误。
5. **期货代码去连股票通道**：TqSimStock 收期货代码会失败；反之亦然。
6. **把回测当实盘跑**：回测里 `wait_update()` 由时光机推进；实盘里它等服务器推送，两者时序语义不同，逻辑要能兼容。

## 交接给阶段 3 的内容（原样传递）

```
- 可运行的策略代码（含 selftest / dry-run / export / verify 四种模式）
- 参数表实现（命令行参数定义）
- 单元测试与测试输出
- requirements.lock.txt
- 已知偏差清单（哪些假设没建模）
```
