# Windows App Workflow —— Skill 包使用说明

一套从 [agency-agents](../../README.md) 仓库提炼出的**参数化工作流 skills**，用于从零开发任意主题的 Windows 桌面软件（.exe / 安装包 / 绿色版）。主题每次不同没关系——所有可变内容都做成了启动变量。

## 目录结构

```
windows-app-workflow/
├── SKILL.md                    # ★ 主入口：流程总览 + 一键启动模板 + 全局铁律
├── 01-requirements/SKILL.md    # 需求发现与产品定义（PRD / RICE / GO-NO-GO）
├── 02-architecture/SKILL.md    # 技术选型与架构（Electron/Tauri/原生、IPC 边界、ADR）
├── 03-ui-ux-design/SKILL.md    # 设计系统、组件清单、主题、Windows 平台约定
├── 04-foundation/SKILL.md      # Git 规范、CI、签名基础设施、walking skeleton
├── 05-implementation/SKILL.md  # Sprint 循环、安全编码、足迹预算、代码审查
├── 06-testing/SKILL.md         # 自动化测试、性能、无障碍、Reality Check 验收
├── 07-security/SKILL.md        # STRIDE 威胁建模、安全审查、密钥管理
├── 08-packaging-release/SKILL.md # 打包、代码签名、自动更新、灰度发布
├── 09-docs-operations/SKILL.md # 文档、i18n、崩溃分诊、运营节奏
└── templates.md                # PRD/ADR/契约/检查表等全部模板
```

## 怎么用

**方式 1：整条流水线**
打开 `SKILL.md`，复制"一键启动模板"，填上项目变量，粘贴给任意 AI 编码助手（Claude Code、Cursor、Codex、Gemini CLI 等），让它按阶段 1→8 执行。

**方式 2：单阶段调用**
已经做到一半的项目，直接读对应阶段的 `SKILL.md` 并让 AI 按其中的清单执行。例如只缺发布环节 → 只跑 `08-packaging-release`。

**方式 3：作为 Claude Code / Cursor 的 skills**
把整个 `windows-app-workflow/` 目录复制到你的项目或全局 skills 目录（如 `.claude/skills/`），frontmatter 中的 `description` 会帮助助手在合适时机自动路由；也可以手动 `@` 引用。

## 设计原则

1. **证据优先**：所有质量门默认 "NEEDS WORK"，通过必须附证据（继承自 Reality Checker）。
2. **签名先于功能**：发布链路在第一个功能之前走通（继承自 Desktop App Engineer）。
3. **参数化**：主题、功能、时间线全部是变量，skill 本身不含任何具体产品假设。
4. **自我进化**：每次发布后用 `templates.md` 的 T8 模板复盘，把踩坑写回对应 skill。

## 与源仓库的关系

本包是 `engineering/`、`design/`、`testing/`、`security/`、`product/`、`strategy/playbooks/` 等目录中 20+ 个 agent 定义与 NEXUS 阶段流水线的**压缩与重组**，面向"单人 + AI 做 Windows 软件"场景。想读完整角色设定（人格、进阶能力、沟通风格），回源文件查看，映射表见 `SKILL.md` 第六节。
