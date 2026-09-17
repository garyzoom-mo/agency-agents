#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""example_backtest —— TqSdk 回测骨架：跑得动只是及格线，结论必须带证据。

这个文件刻意把「策略逻辑」和「回测管道」分开，并把三件最容易被跳过的事做进代码里：

1. **不用未来数据**：只用已收盘的 K 线（iloc[:-1]）决策，绝不碰刚生成的那根未完成 K 线。
2. **成本与成交假设写进结论**：模拟账户手续费、有无滑点建模、决策/成交时点，全部记录在案。
3. **结论落成文件**：每次回测输出一份 JSON（含参数、区间、统计量、代码版本），
   否则"我回测过了"这句话三个月后没人能复核。

    python example_backtest.py --check          # 不联网，校验参数与依赖，打印运行计划
    TQ_USER=xxx TQ_PASS=yyy python example_backtest.py --start 2023-01-01 --end 2024-12-31

注意：TqBacktest 强制要求模拟账户（TqSim / TqSimStock），实盘账户不能进回测。
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))

DEFAULT_SYMBOLS = ["SSE.600519", "SZE.300750", "SSE.601318"]

# 回测结论的"证据"落盘目录：仓库里请 gitignore，别把一堆报告提交进去
REPORT_DIR = Path(os.getenv("TQ_REPORT_DIR", "reports"))


def _dt(value: str) -> date:
    return datetime.strptime(value, "%Y-%m-%d").date()


def build_plan(symbols: List[str], start: date, end: date, params: Dict[str, float]) -> Dict:
    """把这次回测的全部前提写成可存档的结构。回测报告缺任何一项都不算完整。"""
    try:
        import tqsdk

        sdk_version = tqsdk.__version__
        sdk_ok = True
    except ImportError:
        sdk_version = None
        sdk_ok = False

    return {
        "run_at": datetime.now().isoformat(timespec="seconds"),
        "engine": {"name": "tqsdk", "version": sdk_version, "installed": sdk_ok},
        "python": platform.python_version(),
        "period": {"start": start.isoformat(), "end": end.isoformat()},
        "symbols": symbols,
        "params": params,
        "data": {"kline": "日线", "adjust": "前复权(adj_type='F')", "used_bars": "只用到已收盘K线"},
        "execution_assumptions": {
            "decision_time": "当日收盘后（用已收盘K线算信号）",
            "fill_time": "下一根K线（TqBacktest 内为次日首个 tick，近似次日开盘）",
            "commission": "模拟账户按行情里的手续费计算；期货可用 sim.set_commission 覆盖，股票账户(3.10.x)无此接口",
            "slippage": "未建模 —— 必须另做成本敏感性分析，不要只看这里的收益数字",
            "limit_up_down": "未显式建模涨跌停与停牌导致的不可成交",
            "t_plus_1": "未显式建模（A 股 T+1 约束请人工核对信号频率）",
        },
        "known_gaps": [
            "单一区间、单组参数的结果不能称为'策略有效'，需要走 03-validation 的稳健性检查",
            "样本外/滚动窗口验证未包含在本脚本内",
        ],
    }


def run_backtest(symbols: List[str], start: date, end: date, params: Dict[str, float],
                 *, init_balance: float = 1_000_000.0, volume: int = 1000,
                 fast: int = 5, slow: int = 20) -> Dict:
    """双均线 + 波动率过滤的多标的股票回测（逻辑与 example_strategy 保持一致）。

    返回：统计 + 计划（供落盘）。
    """
    from tqsdk import BacktestFinished, TqApi, TqAuth, TqBacktest, TqSimStock
    from tqsdk.tafunc import time_to_datetime

    from example_strategy import compute_position_signals

    user, password = os.getenv("TQ_USER"), os.getenv("TQ_PASS")
    if not user or not password:
        raise SystemExit("缺少环境变量 TQ_USER / TQ_PASS（快期账号），不要写进代码里")

    plan = build_plan(symbols, start, end, params)
    account = TqSimStock(init_balance=init_balance)
    stats: Dict[str, object] = {}

    api = TqApi(account, backtest=TqBacktest(start_dt=start, end_dt=end), auth=TqAuth(user, password))
    try:
        klines = {
            s: api.get_kline_serial(s, 86400, data_length=max(slow * 3, 120), adj_type="F")
            for s in symbols
        }
        from tqsdk import TargetPosTask

        targets = {s: TargetPosTask(api, s) for s in symbols}
        last_day_by_symbol: Dict[str, int] = {}

        while True:
            api.wait_update()
            for symbol, kline in klines.items():
                if not api.is_changing(kline.iloc[-1], "datetime"):
                    continue

                # 关键：最后一根是刚生成的未完成 K 线，决策只能用 iloc[:-1]
                closed = kline.iloc[:-1]
                bars = [
                    (int(time_to_datetime(int(ts)).strftime("%Y%m%d")), float(c))
                    for ts, c in zip(closed["datetime"], closed["close"])
                    if int(ts) > 0
                ]
                series = compute_position_signals(bars, fast=fast, slow=slow)
                if not series:
                    continue
                last_day = max(series)
                if last_day_by_symbol.get(symbol) == last_day:
                    continue  # 同一根K线只决策一次，避免重复下单
                last_day_by_symbol[symbol] = last_day

                position = series[last_day]
                targets[symbol].set_target_volume(int(volume * position))
    except BacktestFinished:
        stats = dict(getattr(account, "tqsdk_stat", {}) or {})
    finally:
        api.close()

    return {"plan": plan, "stats": stats}


def write_report(payload: Dict) -> Path:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    path = REPORT_DIR / f"backtest_{datetime.now():%Y%m%d_%H%M%S}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return path


def print_summary(stats: Dict, name: str = "回测结果") -> None:
    if not stats:
        print(f"{name}: 无统计输出（回测是否真的跑完了？）")
        return
    labels = {
        "ror": ("收益率", "{:.2%}"),
        "annual_yield": ("年化收益率", "{:.2%}"),
        "max_drawdown": ("最大回撤", "{:.2%}"),
        "sharpe_ratio": ("夏普", "{:.4f}"),
        "sortino_ratio": ("索提诺", "{:.4f}"),
        "winning_rate": ("胜率", "{:.2%}"),
        "profit_loss_ratio": ("盈亏比", "{:.2f}"),
        "commission": ("手续费合计", "{:.2f}"),
    }
    print(f"===== {name} =====")
    for key, (label, fmt) in labels.items():
        value = stats.get(key)
        if value is None:
            continue
        try:
            print(f"{label:>8}: {fmt.format(float(value))}")
        except (TypeError, ValueError):
            print(f"{label:>8}: {value}")


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="TqSdk 回测骨架（带证据落盘）")
    parser.add_argument("--symbols", default=",".join(DEFAULT_SYMBOLS))
    parser.add_argument("--start", default="2023-01-01")
    parser.add_argument("--end", default="2024-12-31")
    parser.add_argument("--fast", type=int, default=5)
    parser.add_argument("--slow", type=int, default=20)
    parser.add_argument("--volume", type=int, default=1000, help="每次调仓股数（一手=100股）")
    parser.add_argument("--init-balance", type=float, default=1_000_000.0)
    parser.add_argument("--check", action="store_true", help="不联网：只校验依赖与参数，打印运行计划")
    args = parser.parse_args(argv)

    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
    start, end = _dt(args.start), _dt(args.end)
    params = {"fast": args.fast, "slow": args.slow, "volume": args.volume,
              "init_balance": args.init_balance}

    if end <= start:
        print("--end 必须晚于 --start", file=sys.stderr)
        return 2
    if args.fast >= args.slow:
        print("--fast 必须小于 --slow，否则信号恒为 1，回测结果毫无意义", file=sys.stderr)
        return 2

    plan = build_plan(symbols, start, end, params)
    if args.check:
        print(json.dumps(plan, ensure_ascii=False, indent=2, default=str))
        if not plan["engine"]["installed"]:
            print("\n[提示] 当前环境没装 tqsdk，只能看计划；真跑请先 pip install tqsdk pandas", file=sys.stderr)
            return 1
        print("\n[check] 依赖与参数校验通过（未联网、未产生任何交易）")
        return 0

    payload = run_backtest(symbols, start, end, params, init_balance=args.init_balance,
                           volume=args.volume, fast=args.fast, slow=args.slow)
    print_summary(payload["stats"])
    path = write_report(payload)
    print(f"\n证据已落盘：{path}")
    print("下一步（缺了就别谈上线）：样本外区间、参数敏感性、成本敏感性、跨标的稳健性。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
