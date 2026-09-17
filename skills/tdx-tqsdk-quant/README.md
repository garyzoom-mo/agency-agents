# 通达信 × TqSdk 量化工作流 —— Skill 包使用说明

一套从 [agency-agents](../../README.md) 仓库提炼出的**参数化量化工作流 skills**：用 Python（主线是 [TqSdk 天勤量化](https://www.shinnytech.com/products/tqsdk)）做策略研究与信号计算，把结果桥接进**通达信**做盯盘、选股、预警与下单。

## 目录结构

```
tdx-tqsdk-quant/
├── SKILL.md                    # ★ 主入口：流程总览 + 项目变量 + 一键启动模板 + 全局铁律
├── 01-strategy-spec/SKILL.md   # 策略规格书：假设、规则、参数、能力边界、桥接方式决策
├── 02-tqsdk-research/SKILL.md  # TqSdk 工程骨架：数据获取、指标、参数外置、幂等、日志
├── 03-validation/SKILL.md      # 回测有效性：未来函数自检、成本假设、样本外、稳健性、证据门
├── 04-signal-export/SKILL.md   # 信号落盘：三条通道的精确格式、原子写、写后校验、故障对照表
├── 05-tdx-formula/SKILL.md     # 通达信接入：公式函数、选股、预警、语义陷阱、DLL 进阶
├── 06-live-ops/SKILL.md        # 实盘运维：定时任务、审计留痕、对账、停机开关、事故处置
├── templates.md                # 模板：策略规格书、回测报告、上线检查表、公式模板、故障对照、复盘
└── reference/                  # ★ 可直接跑的参考实现（纯标准库，49 项测试全绿）
    ├── tdx_bridge.py
    ├── test_tdx_bridge.py
    ├── example_strategy.py
    ├── example_backtest.py
    └── test_example_strategy.py
```

## 三分钟上手

```bash
cd skills/tdx-tqsdk-quant/reference

# 1) 不联网、不碰通达信：逻辑自检（含未来函数检测）
python example_strategy.py selftest

# 2) 全链路空跑：自动造一个临时通达信目录，写完后读回校验
python example_strategy.py demo

# 3) 全量单元测试（纯标准库，不需要 tqsdk / 通达信）
python -m unittest discover -s . -p "test_*.py"
```

```bash
# 4) 真实环境：先把你的通达信数据号在「公式系统 → 自定义数据管理器」里建好
python example_strategy.py export --tdx-dir "C:/new_tdx" --dry-run   # 只看计划
TQ_USER=xxx TQ_PASS=yyy python example_strategy.py export --tdx-dir "C:/new_tdx"
python example_strategy.py verify --tdx-dir "C:/new_tdx"             # 只读校验
```

然后在通达信里写一行公式即可看到 Python 的信号（详见 `05-tdx-formula/SKILL.md`）：

```
SIG:SIGNALS_USER(100,0);
```

## 怎么用

**方式 1：整条流水线**
打开 `SKILL.md`，复制"一键启动模板"，填上项目变量，粘贴给任意 AI 编码助手（Claude Code、Cursor、Codex、Gemini CLI 等），让它按阶段 1→6 执行。

**方式 2：单阶段调用**
已经做到一半的项目，直接读对应阶段的 `SKILL.md`。例如只有"信号写不进通达信"这一个问题 → 只跑 `04-signal-export`。

**方式 3：作为 Claude Code / Cursor 的 skills**
把整个 `tdx-tqsdk-quant/` 目录复制到项目或全局 skills 目录（如 `.claude/skills/`），frontmatter 的 `description` 会帮助助手自动路由；也可以手动引用。

**方式 4：只拿代码**
`reference/tdx_bridge.py` 是零依赖模块，可以单独拷进你自己的项目：三条落盘通道（外部数据 / 序列数据 / 板块）、幂等重写、原子落盘、写后读回校验、GBK 编码校验、CLI 一把梭。

## 设计原则

1. **证据优先**：每个质量门默认 NEEDS WORK，通过必须附证据（读回校验输出、回测 JSON、通达信截图）。
2. **桥接最小化**：能用文件桥就不上 DLL；能用官方 txt 导入就不写二进制；能只出信号就不碰下单接口。
3. **幂等 + 原子**：重复跑结果一致，通达信永远读不到写了一半的文件。
4. **诚实标注边界**：非官方文档的机制（如 `.dat` 二进制布局）明确标出来，并给出本机验证方法。
5. **自我进化**：每次上线/事故后用 `templates.md` 的复盘模板，把踩坑写回对应 skill。

## 与源仓库的关系

本 skill 包是**提炼物**，不替代源 agent 文件：需要某个角色的完整人格与方法论（例如 Reality Checker 的完整验收流程、Statistician 的统计推理框架）时，请直接读 `agency-agents` 里对应的 `.md`。来源清单见 `SKILL.md` 第七节。

## 免责声明

本工作流只解决工程可靠性问题（数据对不对、写没写进去、有没有用未来数据、失败了会不会吵），**不构成任何投资建议**，也不对策略盈亏负责。实盘交易前请自行评估风险，并优先以"只出信号、人工确认"的方式运行。
