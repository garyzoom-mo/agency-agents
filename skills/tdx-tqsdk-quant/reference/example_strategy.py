#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""example_strategy —— TqSdk 策略信号 → 通达信的最小可运行闭环。

策略本身刻意选最朴素的「双均线趋势 + 波动率过滤」，重点演示工程骨架，而不是策略本身：
    1. 参数外置（命令行 / 环境变量），代码里不写死标的和参数
    2. 用已收盘的 K 线算信号（iloc[-2]），绝不碰未完成的最后一根
    3. 同一份信号同时产出三种交付物：序列数据 / 外部数据 / 板块清单
    4. 写盘走 tdx_bridge（幂等 + 原子 + 写后读回校验），失败时退出码非 0
    5. --dry-run 先空跑，不碰通达信目录 —— 上线前必须先跑这个

四种模式
    demo     纯离线，合成数据走完整链路（不需要天勤账号、不需要通达信）—— 拿它验证环境
    selftest 只跑逻辑自检：未来函数（前缀稳定性）+ 信号取值范围
    export   用 TqSdk 拉历史日线 → 算信号 → 导出（收盘后跑，T+1 使用）
    live     盘中订阅实时日线，每根日线收盘后写一次信号
    verify   只读回通达信目录里已写入的内容，校验数据是否真的落对了

凭据：TQ_USER / TQ_PASS 环境变量（快期账号），绝不写进代码或仓库。
用法：
    python example_strategy.py demo   --tdx-dir /tmp/new_tdx
    python example_strategy.py export --tdx-dir "C:/new_tdx" --symbols SSE.600519,SZE.300750
    python example_strategy.py live   --tdx-dir "C:/new_tdx" --run-hours 6   # 试跑 6 小时
"""
from __future__ import annotations

import argparse
import os
import sys
import tempfile
import time
from datetime import date, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))

from tdx_bridge import TdxBridge, TdxBridgeError, normalize_date  # noqa: E402

DEFAULT_SYMBOLS = ["SSE.600519", "SZE.300750", "SSE.601318"]

# 数据号：与通达信「自定义数据管理器」里手工建好的编号必须一致
SERIES_ID_POSITION = 100  # 序列数据：每日目标仓位（1=多 / 0=空仓 / -1=空头）
DATA_ID_NOTE = 101        # 外部数据：今日动作的文字说明
BLOCK_NAME = "TQ_SIGNAL"  # 板块：今日选出的股票池


# ---------------------------------------------------------------------------
# 策略参数与信号计算（纯函数，便于单测和复用）
# ---------------------------------------------------------------------------


def compute_position_signals(bars: List[Tuple[int, float]], *, fast: int = 5, slow: int = 20,
                             vol_window: int = 20, vol_max: float = 0.05) -> Dict[int, float]:
    """双均线 + 波动率过滤。

    :param bars: [(YYYYMMDD, 收盘价)]，按日期升序，且**只含已收盘的 K 线**
    :return: {YYYYMMDD: 目标仓位}，1.0 持多 / 0.0 空仓

    波动率 = 近 vol_window 日收益率的样本标准差（日频）。超过 vol_max 就不开新仓，
    这是最便宜的「别在剧烈波动里硬上」风控。
    """
    if len(bars) < slow + 2:
        return {}

    dates = [d for d, _ in bars]
    closes = [c for _, c in bars]
    signals: Dict[int, float] = {}

    fast_ma: List[Optional[float]] = [None] * len(closes)
    slow_ma: List[Optional[float]] = [None] * len(closes)
    for i in range(len(closes)):
        if i + 1 >= fast:
            fast_ma[i] = sum(closes[i + 1 - fast:i + 1]) / fast
        if i + 1 >= slow:
            slow_ma[i] = sum(closes[i + 1 - slow:i + 1]) / slow

    for i in range(1, len(closes)):
        if fast_ma[i] is None or slow_ma[i] is None:
            continue
        rets = [
            closes[j] / closes[j - 1] - 1
            for j in range(max(1, i - vol_window + 1), i + 1)
            if closes[j - 1]
        ]
        vol = _stdev(rets) if len(rets) >= 2 else 0.0
        trend_up = fast_ma[i] > slow_ma[i]
        signals[dates[i]] = 1.0 if (trend_up and vol <= vol_max) else 0.0

    return signals


def _stdev(values: List[float]) -> float:
    n = len(values)
    mean = sum(values) / n
    return (sum((v - mean) ** 2 for v in values) / (n - 1)) ** 0.5


def assert_prefix_stable(bars: List[Tuple[int, float]], compute=compute_position_signals,
                         *, probe_every: int = 5, min_bars: int = 60) -> int:
    """未来函数自检：用「前 t 根」算出的历史信号，必须与用全量数据算出的同一天信号完全一致。

    原理：合法策略在 t 日的信号只依赖 t 日及之前的数据。如果代码里偷用了全样本信息
    （例如按全期最大值做归一化、用了 shift(-1)、用了未收盘的最后一根 K 线），
    全量计算会"污染"历史信号，这里就能抓到第一处违约。

    :return: 实际比对的信号点数量
    :raises AssertionError: 一旦发现不一致，报出日期与两个取值
    """
    if len(bars) < min_bars + 2:
        raise AssertionError(f"样本不足：需要至少 {min_bars + 2} 根 K 线，实际 {len(bars)}")

    full = compute(bars)
    checked = 0
    for t in range(min_bars, len(bars) + 1, probe_every):
        partial = compute(bars[:t])
        for day, value in partial.items():
            expected = full.get(day)
            if expected is None or value != expected:
                raise AssertionError(
                    f"检测到未来函数：{day} 在全量数据下为 {expected}，"
                    f"但只用前 {t} 根K线时算出 {value}"
                )
            checked += 1
    return checked


def action_text(position: float, prev_position: Optional[float]) -> str:
    if prev_position is None or position != prev_position:
        return "开多" if position == 1.0 else "平仓"
    return "持有" if position == 1.0 else "空仓"


# ---------------------------------------------------------------------------
# 三种交付物
# ---------------------------------------------------------------------------


def export_to_tdx(bridge: Optional[TdxBridge], signals_by_symbol: Dict[str, Dict[int, float]],
                  *, dry_run: bool = False) -> int:
    """把信号写进通达信，返回今日触发开仓的品种数。

    写三样东西：
      * 序列数据：每只票的每日目标仓位 → 公式 SIGNALS_USER(100,0) 取用
      * 外部数据：最新一次动作的文字说明 → 公式 EXTERNSTR(0,101) 取用
      * 板块文件：今日信号为 1 的股票清单 → 通达信里当自选/选股池用
    """
    if dry_run:
        # dry-run 的契约：不校验、不创建、不写入 —— 通达信目录可以根本不存在
        for symbol, series in signals_by_symbol.items():
            if not series:
                print(f"[dry-run] {symbol}: 无有效信号")
                continue
            last_day = max(series)
            print(f"[dry-run] {symbol}: 将写 {len(series)} 条序列数据，最新 {last_day} → 仓位 {series[last_day]:g}")
        print(f"[dry-run] 共 {len(signals_by_symbol)} 个品种；未写入任何文件")
        return 0

    assert bridge is not None, "非 dry-run 模式必须传入 TdxBridge"

    extern_rows = []
    picked: List[str] = []

    for symbol, series in signals_by_symbol.items():
        if not series:
            print(f"[skip] {symbol}: 有效数据不足，未生成信号", file=sys.stderr)
            continue

        count = bridge.write_user_series(symbol, series, series_id=SERIES_ID_POSITION)
        back = bridge.read_user_series(symbol, series_id=SERIES_ID_POSITION)
        assert len(back) == count, f"{symbol} 写后校验失败：写 {count} 读回 {len(back)}"

        days = sorted(series)
        last_day, position = days[-1], series[days[-1]]
        prev_position = series[days[-2]] if len(days) > 1 else None
        text = f"{date(last_day // 10000, last_day // 100 % 100, last_day % 100):%m-%d} {action_text(position, prev_position)}"
        extern_rows.append((symbol, text, position, DATA_ID_NOTE))

        if position == 1.0:
            picked.append(symbol)
        print(f"[OK] {symbol}: {count} 条序列数据，最新 {last_day} → 仓位 {position:g}（{text}）")

    bridge.write_extern_user(extern_rows, merge=True)
    if picked:
        bridge.write_block(BLOCK_NAME, picked)
    print(f"[OK] 外部数据 {len(extern_rows)} 条；板块 {BLOCK_NAME}: {len(picked)} 只")
    return len(picked)


# ---------------------------------------------------------------------------
# 数据来源：demo / export / live
# ---------------------------------------------------------------------------


def _synthetic_bars(symbol: str, days: int = 120) -> List[Tuple[int, float]]:
    """确定性合成日线（同一个 symbol 永远得到同一组数据），用于离线自检。"""
    seed = sum(ord(c) for c in symbol)
    trading_days: List[date] = []
    cursor = date.today()
    while len(trading_days) < days:
        if cursor.weekday() < 5:  # 跳过周末，贴近交易日
            trading_days.append(cursor)
        cursor -= timedelta(days=1)
    trading_days.reverse()  # 升序，且最后一根 = 今天

    price = 20.0 + seed % 30
    bars: List[Tuple[int, float]] = []
    for i, day in enumerate(trading_days, start=1):
        drift = 0.004 if i % 37 < 25 else -0.006  # 周期性趋势，两段行情都出现
        wiggle = ((seed * i) % 17 - 8) / 1000
        price = max(1.0, price * (1 + drift + wiggle))
        bars.append((normalize_date(day), round(price, 3)))
    return bars


def load_bars_demo(symbol: str) -> List[Tuple[int, float]]:
    return _synthetic_bars(symbol)


def load_bars_tqsdk(symbols: List[str], days: int, *, auth) -> Dict[str, List[Tuple[int, float]]]:
    """用 TqSdk 下载历史日线（收盘后批处理用这个）。"""
    try:
        from tqsdk import TqApi
    except ImportError:  # pragma: no cover
        raise SystemExit("未安装 tqsdk：pip install tqsdk pandas（或先用 demo 模式验证环境）")

    end_dt = date.today()
    start_dt = end_dt - timedelta(days=int(days * 1.8))
    out: Dict[str, List[Tuple[int, float]]] = {}

    api = TqApi(auth=auth)  # 只下载数据，不需要交易账户
    try:
        for symbol in symbols:
            df = api.get_kline_data_series(symbol, 86400, start_dt=start_dt, end_dt=end_dt,
                                           adj_type="F")
            # 只用已收盘的 K 线：最后一根可能是当天未收盘的，丢掉
            bars = [(normalize_date(int(row["datetime"])), float(row["close"])) for _, row in df.iterrows()]
            out[symbol] = bars[:-1] if bars else []
            print(f"[data] {symbol}: {len(out[symbol])} 根日线（已剔除未收盘K线）")
    finally:
        api.close()
    return out


def load_bars_live(symbols: List[str], *, auth, tdx_dir: Path,
                   run_once_hours: Optional[float] = None) -> None:
    """盘中订阅：每根新日线出现（即当日收盘）后写一次信号。

    这是生产模式，特征：只写不交易（信号给人看/给通达信预警用），
    每次写盘都留日志；异常直接抛出，让任务计划程序看到失败。
    """
    from tqsdk import TqApi
    from tqsdk.tafunc import time_to_datetime

    api = TqApi(auth=auth)
    bridge = TdxBridge(tdx_dir, series_id=SERIES_ID_POSITION)
    klines = {s: api.get_kline_serial(s, 86400, data_length=120, adj_type="F") for s in symbols}
    started = time.time()
    last_written: Dict[str, int] = {}

    try:
        while True:
            api.wait_update()
            for symbol, kline in klines.items():
                if not api.is_changing(kline.iloc[-1], "datetime"):
                    continue
                # K 线刚生成：最后一根是新的未完成 K 线，用它前面的收盘数据
                closed = kline.iloc[:-1]
                bars = [
                    (normalize_date(time_to_datetime(int(ts))), float(c))
                    for ts, c in zip(closed["datetime"], closed["close"])
                    if int(ts) > 0
                ]
                series = compute_position_signals(bars)
                if not series:
                    continue
                last_day = max(series)
                if last_written.get(symbol) == last_day:
                    continue  # 幂等：同一天不重复写
                count = bridge.write_user_series(symbol, series)
                last_written[symbol] = last_day
                print(f"[{time.strftime('%H:%M:%S')}] {symbol}: 写入 {count} 条，最新 {last_day} 仓位 {series[last_day]:g}")
            if run_once_hours and time.time() - started > run_once_hours * 3600:
                print("[live] 达到运行时长上限，正常退出")
                return
    finally:
        api.close()


# ---------------------------------------------------------------------------
# verify
# ---------------------------------------------------------------------------


def verify(tdx_dir: Path, symbols: List[str]) -> int:
    """只读校验：确认通达信目录里真的有我们期望的数据。"""
    bridge = TdxBridge(tdx_dir, series_id=SERIES_ID_POSITION)
    problems = 0
    for symbol in symbols:
        try:
            rows = bridge.read_user_series(symbol, series_id=SERIES_ID_POSITION)
        except TdxBridgeError as exc:
            print(f"[FAIL] {symbol}: {exc}")
            problems += 1
            continue
        if not rows:
            print(f"[FAIL] {symbol}: 文件存在但 0 条记录（公式会取到空值）")
            problems += 1
            continue
        last_day, last_value = rows[-1]
        gap = (date.today() - date(last_day // 10000, last_day // 100 % 100, last_day % 100)).days
        flag = "WARN" if gap > 7 else "OK"
        if gap > 7:
            problems += 1
        print(f"[{flag}] {symbol}: {len(rows)} 条，最新 {last_day}={last_value:g}（距今 {gap} 天）")
    return problems


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="TqSdk 信号 → 通达信（示例）")
    parser.add_argument("mode", choices=["demo", "export", "live", "verify", "selftest"])
    parser.add_argument("--tdx-dir", help="通达信安装目录；demo 模式可省略（自动用临时目录）")
    parser.add_argument("--symbols", default=",".join(DEFAULT_SYMBOLS), help="逗号分隔的合约代码")
    parser.add_argument("--days", type=int, default=180, help="下载/合成多少根日线")
    parser.add_argument("--fast", type=int, default=5)
    parser.add_argument("--slow", type=int, default=20)
    parser.add_argument("--vol-max", type=float, default=0.05, help="开仓允许的最大日波动率")
    parser.add_argument("--run-hours", type=float, default=None, help="live 模式最长运行小时数（便于试跑）")
    parser.add_argument("--dry-run", action="store_true", help="只打印将要写入的内容，不落盘")
    args = parser.parse_args(argv)

    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]

    # 目录策略：selftest 完全不碰文件系统；demo 缺 --tdx-dir 时自动用临时目录；其余模式必须显式给
    tmp_dir: Optional[tempfile.TemporaryDirectory] = None
    tdx_dir: Optional[Path] = None
    if args.mode == "selftest":
        tdx_dir = None
    elif args.mode == "demo" and not args.tdx_dir:
        tmp_dir = tempfile.TemporaryDirectory()
        tdx_dir = Path(tmp_dir.name) / "new_tdx"
        (tdx_dir / "T0002" / "signals").mkdir(parents=True)
        (tdx_dir / "T0002" / "blocknew").mkdir(parents=True)
        print(f"[demo] 使用临时通达信目录：{tdx_dir}")
    elif not args.tdx_dir:
        print(f"{args.mode} 模式必须给 --tdx-dir（真实通达信安装目录）", file=sys.stderr)
        return 2
    else:
        tdx_dir = Path(args.tdx_dir)

    try:
        if args.mode == "selftest":
            # 只做逻辑自检，不写任何通达信文件
            checked = 0
            for symbol in symbols:
                bars = load_bars_demo(symbol)
                checked += assert_prefix_stable(bars, lambda b: compute_position_signals(
                    b, fast=args.fast, slow=args.slow, vol_max=args.vol_max))
                signals = compute_position_signals(bars, fast=args.fast, slow=args.slow, vol_max=args.vol_max)
                assert all(v in (0.0, 1.0) for v in signals.values()), f"{symbol} 出现非法仓位值"
                print(f"[selftest] {symbol}: {len(bars)} 根K线 / {len(signals)} 个信号，前缀稳定性检查通过")
            print(f"[selftest] 无未来函数，共校验 {checked} 个信号点")
            return 0

        if args.mode == "verify":
            assert tdx_dir is not None
            return 1 if verify(tdx_dir, symbols) else 0

        assert tdx_dir is not None, "该模式必须解析出通达信目录"
        if args.mode == "demo":
            bars_by_symbol = {s: load_bars_demo(s) for s in symbols}
        elif args.mode == "export":
            user, password = os.getenv("TQ_USER"), os.getenv("TQ_PASS")
            if not user or not password:
                print("缺少环境变量 TQ_USER / TQ_PASS（快期账号）", file=sys.stderr)
                return 2
            from tqsdk import TqAuth

            bars_by_symbol = load_bars_tqsdk(symbols, args.days, auth=TqAuth(user, password))
        else:  # live
            user, password = os.getenv("TQ_USER"), os.getenv("TQ_PASS")
            if not user or not password:
                print("缺少环境变量 TQ_USER / TQ_PASS（快期账号）", file=sys.stderr)
                return 2
            from tqsdk import TqAuth

            load_bars_live(symbols, auth=TqAuth(user, password), tdx_dir=tdx_dir,
                           run_once_hours=args.run_hours)
            return 0

        signals = {
            symbol: compute_position_signals(bars, fast=args.fast, slow=args.slow, vol_max=args.vol_max)
            for symbol, bars in bars_by_symbol.items()
        }
        bridge = None if args.dry_run else TdxBridge(tdx_dir, series_id=SERIES_ID_POSITION)
        picked = export_to_tdx(bridge, signals, dry_run=args.dry_run)

        if args.mode == "demo" and not args.dry_run:
            print("\n[verify] 读回校验：")
            problems = verify(tdx_dir, symbols)
            print(f"\n[demo] 完成。挑出 {picked} 只标的。"
                  f"在真实环境里，此时通达信公式 SIGNALS_USER({SERIES_ID_POSITION},0) 就能取到这些值。")
            return 1 if problems else 0
        return 0
    finally:
        if tmp_dir is not None:
            tmp_dir.cleanup()


if __name__ == "__main__":
    raise SystemExit(main())
