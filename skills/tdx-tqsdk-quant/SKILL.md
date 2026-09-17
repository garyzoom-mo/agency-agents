---
name: tdx-tqsdk-quant
description: 通达信 × TqSdk 量化工作流（Python 为主线）。适用场景：用 Python/TqSdk 写量化策略（选股、择时、信号、组合），并把结果接进通达信（自定义数据、自定义序列数据、自定义板块、公式、预警），让通达信当下单/盯盘/复盘终端。覆盖策略规格 → TqSdk 研究 → 回测有效性验证 → 信号导出落盘 → 通达信公式接入 → 实盘运维全流程。提炼自 agency-agents 仓库的工程/测试/统计/金融类 20+ 专家 agent，并附一套纯标准库、已通过 49 项单元测试的参考实现。
---

# 通达信 × TqSdk 量化工作流（主入口）

> 目标：**让 Python 负责算，让通达信负责看和下单。**
> 本工作流参数化：标的池、策略、数据号每次注入，按 6 个阶段顺序执行，每阶段过质量门才放行。

## 一、项目变量（每次启动新策略时填写）

| 变量 | 说明 | 示例 |
|---|---|---|
| `{{策略名}}` | 一眼能看懂的名字 | 双均线动量轮动 |
| `{{一句话假设}}` | 这个策略为什么赚钱 | 趋势延续：强者恒强，20日新高后跟随 |
| `{{标的池}}` | 股票/ETF/可转债/指数成分 | 沪深300 成分股 |
| `{{信号频率}}` | 日频收盘 / 分钟 | 日频收盘后 |
| `{{通达信版本与目录}}` | 金融终端/专业版 + 安装路径 | V7.642，`C:\new_tdx` |
| `{{通达信数据号}}` | 自定义数据管理器里新建的编号 | 序列数据 100 / 外部数据 101 |
| `{{交付物形态}}` | 图上看 / 选股池 / 预警 / DLL | 主图标记 + 条件选股公式 |
| `{{是否真下单}}` | 只出信号 / 人工确认 / 自动下单 | 只出信号（默认） |
| `{{风控红线}}` | 必须停机的条件 | 单票回撤 >15% 或 3 连亏 |

## 二、流程总览（6 阶段 × 子 skill）

| # | 阶段 | 子 skill | 核心动作 | 质量门（不过不放行） |
|---|------|---------|---------|---------------------|
| 1 | 策略规格 | `01-strategy-spec/` | 假设→可验证规则→参数清单；能力边界（Python 能算什么、TDX 只能取什么）；桥接方式决策 | 规格书写满 8 节，含"什么情况下这个策略应该亏钱" |
| 2 | TqSdk 研究 | `02-tqsdk-research/` | 数据获取、指标计算、工程骨架（参数外置/日志/幂等） | 能在无凭据环境用 `selftest/dry-run` 空跑通过 |
| 3 | 回测验证 | `03-validation/` | 未来函数自检、成本与成交假设、样本外与稳健性、证据化报告 | 前缀稳定性测试通过 + 成本敏感性结论 + 报告落盘 |
| 4 | 信号导出 | `04-signal-export/` | 落盘三通道（外部数据 / 序列数据 / 板块）+ 幂等原子写 + 写后校验 | 写完读回校验 100% 一致，退出码语义正确 |
| 5 | TDX 接入 | `05-tdx-formula/` | 公式编写（SIGNALS_USER / EXTERNSTR）、选股、预警、DLL 进阶 | 通达信里**肉眼看到**信号，且与 Python 输出逐日对齐 |
| 6 | 实盘运维 | `06-live-ops/` | 定时任务、交易日判断、审计留痕、对账、停机开关、事故处置 | 演练过一次"清空信号→通达信回到空仓显示"，且定时任务连跑 3 天成功 |

## 三、一键启动模板（复制后填变量即可）

```
启动 通达信 × TqSdk 量化工作流。

策略名：{{策略名}}
一句话假设：{{一句话假设}}
标的池：{{标的池}}
信号频率：{{信号频率}}
通达信版本与目录：{{通达信版本与目录}}
通达信数据号：{{通达信数据号}}
交付物形态：{{交付物形态}}
是否真下单：{{是否真下单}}
风控红线：{{风控红线}}
Python 环境：{{Python 版本}}，已装 tqsdk / pandas

执行要求：
1. 按阶段 1→6 顺序执行，每阶段开始前读取对应子 skill。
2. 每阶段结束必须用「质量门」表逐项附证据；证据不足 = NEEDS WORK，不放行。
3. 先跑 reference/example_strategy.py selftest 与 demo，确认环境通路，再动真实通达信目录。
4. 阶段 3 未通过就写阶段 4 的盘 —— 视为违规，回退重做。
5. 所有涉及真实账户的操作默认关闭（只出信号），打开必须由人显式确认。
```

## 四、全局铁律（来自各专家 agent 的硬性规则）

1. **证据优先，默认 NEEDS WORK**：任何"策略有效 / 已上线"的结论必须附证据（回测 JSON、读回校验输出、通达信截图、逐日对齐表）。首版实现通常要 2–3 轮返工，一次全绿是危险信号（Reality Checker）。
2. **不用未来数据**：Python 侧只用已收盘 K 线（`iloc[:-1]`），并强制跑「前缀稳定性测试」；通达信侧禁止把 `ZIG/BACKSET/REFX` 一类未来函数写进实盘预警与选股（Statistician + 自建防线）。
3. **先假设，后代码**：写不出"这个策略在什么情况下应该亏钱"，就不许开始写代码（Investment Researcher 的双面论证）。
4. **回测的每个前提都要留痕**：区间、参数、复权口径、成本、成交时点、滑点是否建模、T+1 是否建模。默认不建模的东西必须写在结论里，而不是藏在心里。
5. **幂等 + 原子写**：同一天重复运行结果完全一致；先写临时文件再原子替换，通达信永远读不到半截文件（Data Engineer 的 pipeline 标准）。
6. **信号不等于下单**：默认只产出信号，人工确认后才交易；任何自动下单路径都必须有独立开关和风控红线（SRE + 金融保守默认）。
7. **凭据永不入仓**：`TQ_USER/TQ_PASS` 走环境变量或系统凭据库，日志里脱敏，`.gitignore` 兜底（Secrets Engineer）。
8. **最小改动**：一个 bug 修复只改这个 bug，重构另开分支/另开提交；每个提交可独立回滚（Minimal Change Engineer + Git Workflow Master）。
9. **失败要吵**：脚本失败必须退出码非 0 并写日志；"静悄悄什么都没写"是最危险的失败模式。
10. **诚实标注不确定性**：自定义数据的二进制文件布局、`.blk` 的市场号口径属社区实践而非官方文档，必须在本机实测验证后再依赖（见 `04-signal-export` 的验证清单）。

## 五、单人操盘时的角色兼任表

1 人（你）+ AI 的场景，按此表轮流"戴帽子"（源 agent → 由谁执行）：

| 源 agent | 阶段 | 单人模式下 |
|---|---|---|
| Product Manager 思路 + 本文档 `01` | 1 | AI 出规格书初稿，你确认假设与红线 |
| 量化研究员（对应 Finance Investment Researcher） | 2 | AI 写代码，你定义标的池与参数范围 |
| Academic Statistician + Reality Checker | 3 | AI 出证据，你判断"够不够格上线" |
| Data Engineer + Minimal Change Engineer | 4 | AI 实现并自测，你抽查读回校验结果 |
| 通达信公式侧（无对应 agent，属本地知识） | 5 | AI 写公式，你负责在通达信里肉眼验收 |
| DevOps Automator + SRE | 6 | AI 生成定时任务与日志，你保管凭据、按红线停机 |

## 六、配套参考实现（可直接跑，49 项测试全绿）

```
reference/
├── tdx_bridge.py            # 三条落盘通道的纯标准库实现（无 pandas/numpy 依赖）
├── test_tdx_bridge.py       # 37 项测试：格式字节级断言、幂等、原子写、GBK 编码、错误路径
├── example_strategy.py      # TqSdk 策略骨架：demo / selftest / export / live / verify 五种模式
├── example_backtest.py      # 回测骨架：--check 空跑、统计落盘、成本假设写进结论
└── test_example_strategy.py # 12 项测试：策略逻辑、未来函数自检、端到端导出、dry-run 契约
```

```bash
# 1) 逻辑自检（不联网、不碰通达信）
python example_strategy.py selftest
# 2) 全链路空跑：自动造一个临时通达信目录，跑完打印读回校验
python example_strategy.py demo
# 3) 只看计划不落盘（上线前必做）
python example_strategy.py export --tdx-dir "C:/new_tdx" --dry-run
# 4) 真正导出（收盘后）
TQ_USER=xxx TQ_PASS=yyy python example_strategy.py export --tdx-dir "C:/new_tdx"
# 5) 只读校验：确认通达信目录里真的是今天的数据
python example_strategy.py verify --tdx-dir "C:/new_tdx"
# 6) 回测
TQ_USER=xxx TQ_PASS=yyy python example_backtest.py --start 2023-01-01 --end 2024-12-31
```

## 七、来源映射（本 skill 包提炼自哪些 agent）

- `engineering/engineering-data-engineer.md` — 幂等、schema 契约、显式 null 处理、审计列（落盘通道设计的骨架）
- `engineering/engineering-minimal-change-engineer.md` — 最小 diff、拒绝范围蔓延、每个改动行都要能被任务解释
- `engineering/engineering-code-reviewer.md` — 阻塞项/建议/吹毛求疵三级分类、解释"为什么"
- `engineering/engineering-devops-automator.md` / `engineering/engineering-sre.md` — 定时任务、可观测性、事故处置、失败要吵
- `engineering/engineering-git-workflow-master.md` — 原子提交、分支与回滚纪律
- `testing/testing-test-automation-engineer.md` — 确定性测试、无 sleep、隔离测试数据、flake 归零
- `testing/testing-evidence-collector.md` / `testing-reality-checker.md` — 证据化验收、默认 NEEDS WORK、AUTOMATIC FAIL 触发条件
- `testing/testing-performance-benchmarker.md` / `testing-test-results-analyzer.md` — 基准与结果解读（对应回测绩效与敏感性分析）
- `academic/academic-statistician.md` — 先设计后数据、效应量与区间、多重比较、假设必须显式声明
- `finance/finance-investment-researcher.md` — 论点 vs 叙事、多空对等论证、量化下行、论点失效条件
- `security/security-secrets-credential-engineer.md` — 凭据管理、不入仓、日志脱敏
- `strategy/playbooks/phase-0 ~ phase-6` — 阶段划分与交接包机制

## 八、已知边界（必须诚实告知使用者）

1. **通达信自定义数据的二进制布局（`signals_user_<ID>\<市场>_<代码>.dat`）来自社区长期实践**，非官方文档；不同版本可能不同。`tdx_bridge.py` 同时提供官方 txt 导入路径，正式依赖前请先做 `04-signal-export` 里的本机验证。
2. **自定义数据面向股票/指数/可转债**；期货合约代码（如 `SHFE.rb2510`）不在该机制覆盖内，本工作流会直接拒绝并给出替代方案，而不是写出读不到的数据。
3. **TqSdk 回测不建模滑点**，且股票模拟账户（3.10.x）没有 `set_commission` 接口，涨跌停/停牌/T+1 也不显式建模 —— 所以阶段 3 的成本敏感性与人工核对是硬性要求，不是可选项。
4. 本工作流**不构成投资建议**：它只保证"工程上是可靠的"，不保证"策略是赚钱的"。
