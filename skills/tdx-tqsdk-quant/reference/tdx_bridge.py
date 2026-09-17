#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""tdx_bridge —— 把 Python / TqSdk 策略算出来的信号，写进通达信能读的文件。

只依赖标准库（不需要 pandas / numpy），任何装了 Python 3.8+ 的 Windows 机器都能跑。

三条落盘通道
------------
A. 外部数据（字符串 + 数值，一个品种一条）
   路径: <TDX>\\T0002\\signals\\extern_user.txt
   行格式: 市场|代码|数据号|文字串|数值
   公式: EXTERNSTR(0, 数据号) / EXTERNVALUE(0, 数据号)

B. 自定义序列数据（日期 + 数值，一个品种一条时间序列）
   二进制路径: <TDX>\\T0002\\signals\\signals_user_<数据号>\\<市场>_<代码>.dat
   记录格式: 小端 int32 日期(YYYYMMDD) + 小端 float32 数值，逐条追加存放
   官方导入路径: 生成 "市场|代码|YYYYMMDD|数值" 的 txt，在通达信「自定义数据管理器」里导入
   公式: SIGNALS_USER(数据号, 0)   （0 = 自定义数据，1 = 系统数据）

C. 自定义板块 / 自选股（.blk 列表）
   路径: <TDX>\\T0002\\blocknew\\<板块文件名>.blk
   行格式: 7 字节 —— 市场(0深/1沪) + 6 位代码 + CRLF，GBK 编码

三条设计约束
------------
* 幂等：同一天重复运行，结果完全一致（序列数据是整文件重写，不是追加）。
* 原子：先写同目录临时文件再 os.replace，通达信不会读到写了一半的文件。
* 可验证：write_* 都返回写入条数，并可用 read_* 读回来核对。

免责与边界
----------
* 通达信自定义数据的二进制文件布局来自社区长期实践（非官方文档），不同版本可能略有差异；
  正式使用前先用 read_user_series() 读回校验，或用官方 txt 导入路径（本模块同样提供）。
* 自定义数据面向股票 / 指数 / 可转债；期货合约代码（如 SHFE.rb2510）不在该机制覆盖范围内，
  传入会抛 UnsupportedSymbolError —— 这是刻意的，避免写出通达信读不懂的孤儿数据。

用法见 --help，或直接 import：

    from tdx_bridge import TdxBridge, split_symbol

    b = TdxBridge(r"C:\\new_tdx", series_id=100)
    b.write_user_series("SSE.600519", {20240902: 1.0, 20240903: -1.0})
    b.write_extern_user([("SSE.600519", "多头信号 2024-09-03", 1.0, 100)])
    b.write_block("XG", ["SSE.600519", "SZE.000001"])
"""

from __future__ import annotations

import argparse
import os
import struct
import sys
import tempfile
import time
from datetime import date, datetime
from pathlib import Path
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

__all__ = [
    "TdxBridge",
    "TdxBridgeError",
    "UnsupportedSymbolError",
    "split_symbol",
    "normalize_date",
    "format_value",
    "MARKET_SZ",
    "MARKET_SH",
    "MARKET_BJ",
    "MARKET_NAME",
]

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

MARKET_SZ = 0  # 深圳
MARKET_SH = 1  # 上海
MARKET_BJ = 2  # 北京（部分版本的自定义数据支持，.blk 文件请以本机实测为准）

MARKET_NAME = {MARKET_SZ: "深", MARKET_SH: "沪", MARKET_BJ: "北"}

# 交易所前缀（TqSdk 写法）→ 通达信市场号
EXCHANGE_TO_MARKET = {
    "SSE": MARKET_SH,
    "SH": MARKET_SH,
    "SZE": MARKET_SZ,
    "SZ": MARKET_SZ,
    "BSE": MARKET_BJ,
    "BJ": MARKET_BJ,
}

# 无前缀代码的首位推断规则（顺序敏感：先匹配更具体的前缀）
_CODE_PREFIX_RULES: Tuple[Tuple[Tuple[str, ...], int], ...] = (
    (("6", "9", "5", "11", "13"), MARKET_SH),  # 沪市股票/ETF/沪市可转债(110/113)
    (("0", "3", "1", "2", "12", "15", "16", "18"), MARKET_SZ),  # 深市股票/创业板/深市可转债(12x)/ETF
    (("4", "8", "9"), MARKET_BJ),  # 北交所 43x/83x/87x 等
)

# 通达信 signals 目录下的文件名
EXTERN_USER_FILE = "extern_user.txt"

_GBK_ENCODING = "gbk"  # 通达信文本文件用 GBK(ANSI)，不要用 UTF-8

# 期货交易所前缀：命中即明确拒绝，并给出替代方案
FUTURES_EXCHANGES = frozenset({"SHFE", "DCE", "CZCE", "CFFEX", "INE", "GFEX"})


# ---------------------------------------------------------------------------
# 异常
# ---------------------------------------------------------------------------


class TdxBridgeError(Exception):
    """本模块所有可预期错误的基类。"""


class UnsupportedSymbolError(TdxBridgeError):
    """合约代码无法映射到通达信自定义数据模型（例如期货合约）。"""


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------


def split_symbol(symbol: str) -> Tuple[int, str]:
    """把各种写法的代码统一成通达信的 (市场号, 6位代码)。

    支持：
        "SSE.601456" / "SZE.000001" / "BSE.430047"   （TqSdk 写法）
        "601456.SH"  / "000001.SZ" / "430047.BJ"     （Wind 写法）
        "601456"     / "000001"                      （裸 6 位，按前缀推断市场）

    期货 / 期权等带品种字母的代码会抛 UnsupportedSymbolError。
    """
    raw = (symbol or "").strip().upper()
    if not raw:
        raise UnsupportedSymbolError("空的合约代码")

    head, _, tail = raw.partition(".")

    if tail:  # 带交易所前缀/后缀
        if head in EXCHANGE_TO_MARKET and tail.isdigit():
            exchange, code = head, tail
        elif tail in EXCHANGE_TO_MARKET and head.isdigit():
            exchange, code = tail, head
        elif head in FUTURES_EXCHANGES or not tail.isdigit():
            raise UnsupportedSymbolError(
                f"{symbol!r} 看起来是期货/期权合约：期货不走通达信自定义数据（公式端没有对应的"
                f"引用来读它）。可行替代：① 用主力连续/指数数据在 Python 侧算好选股结果，"
                f"只把股票清单写进 .blk 板块；② 走 TDXDLL 自绘通道。"
            )
        else:
            raise UnsupportedSymbolError(
                f"无法识别的合约代码 {symbol!r}：自定义数据只支持股票/指数/可转债，"
                f"交易所前缀需为 {sorted(EXCHANGE_TO_MARKET)}"
            )
        return EXCHANGE_TO_MARKET[exchange], code.zfill(6)

    if not head.isdigit():
        raise UnsupportedSymbolError(
            f"无法识别的合约代码 {symbol!r}：含有非数字品种字母（如期货 SHFE.rb2510）。"
            f"期货/期权不走通达信自定义数据，请改用其他桥接方式。"
        )

    code = head.zfill(6)
    for prefixes, market in _CODE_PREFIX_RULES:
        if code.startswith(prefixes):
            return market, code
    raise UnsupportedSymbolError(f"无法推断 {symbol!r} 的市场，请显式写成 'SSE.xxxxxx' 形式")


def normalize_date(value) -> int:
    """把日期统一成通达信要的 int YYYYMMDD。

    接受 date / datetime / "2024-09-03" / "20240903" / 20240903 / 时间戳(毫秒或秒)。
    """
    if isinstance(value, datetime):
        return value.year * 10000 + value.month * 100 + value.day
    if isinstance(value, date):
        return value.year * 10000 + value.month * 100 + value.day
    if isinstance(value, bool):
        raise TdxBridgeError(f"非法日期 {value!r}")
    if isinstance(value, int):
        if 19000101 <= value <= 29991231:
            return value
        # TqSdk 的 datetime 字段是纳秒时间戳
        seconds = value / 1_000_000_000 if value > 10**14 else value
        return int(datetime.fromtimestamp(seconds).strftime("%Y%m%d"))
    if isinstance(value, float):
        return normalize_date(int(value))
    if isinstance(value, str):
        text = value.strip().replace("-", "").replace("/", "").replace(".", "")
        if len(text) >= 8 and text[:8].isdigit():
            return int(text[:8])
        raise TdxBridgeError(f"无法解析日期字符串 {value!r}，需要 YYYYMMDD 或 YYYY-MM-DD")
    raise TdxBridgeError(f"无法解析日期 {value!r}（类型 {type(value).__name__}）")


def format_value(value: float, digits: int = 4) -> str:
    """输出紧凑且可解析的数值：去掉多余的 0，避免科学计数法。"""
    text = f"{float(value):.{digits}f}"
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def atomic_write_bytes(path: Path, data: bytes, *, retries: int = 5, delay: float = 0.4) -> None:
    """原子落盘：写同目录临时文件 → os.replace 覆盖目标。

    通达信正在运行时目标文件可能被占用，这里带重试并给出可执行的中文报错。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        last_error: Optional[Exception] = None
        for attempt in range(retries):
            try:
                os.replace(tmp_path, path)
                return
            except PermissionError as exc:  # Windows: 通达信占用中
                last_error = exc
                time.sleep(delay * (attempt + 1))
        raise TdxBridgeError(
            f"写入 {path} 失败（文件被占用）：请先关闭通达信，或在通达信里退出对应数据视图后重试。"
        ) from last_error
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise


def text_to_gbk(text: str, *, where: str = "") -> bytes:
    try:
        return text.encode(_GBK_ENCODING)
    except UnicodeEncodeError as exc:
        raise TdxBridgeError(
            f"{where or '文本'}含 GBK 无法表示的字符（如 emoji / 生僻字）：{exc.object[exc.start:exc.end]!r}。"
            f"通达信文本文件必须是 GBK 编码。"
        ) from exc


# ---------------------------------------------------------------------------
# 主体
# ---------------------------------------------------------------------------


class TdxBridge:
    """通达信信号桥。

    :param tdx_dir:  通达信安装目录（含 T0002 子目录），例如 r"C:\\new_tdx"
    :param series_id: 自定义序列数据的数据号（在通达信「自定义数据管理器」里新建的编号）
    :param block_dir: 板块目录，默认 <tdx_dir>/T0002/blocknew
    """

    def __init__(self, tdx_dir: os.PathLike | str, *, series_id: Optional[int] = None,
                 block_dir: Optional[os.PathLike | str] = None) -> None:
        self.tdx_dir = Path(tdx_dir)
        self.t0002 = self.tdx_dir / "T0002"
        if not self.t0002.is_dir():
            raise TdxBridgeError(
                f"{self.t0002} 不存在：tdx_dir 要指向通达信安装目录（里面应有 T0002 子目录），"
                f"而不是 T0002 本身。"
            )
        self.signals_dir = self.t0002 / "signals"
        self.block_dir = Path(block_dir) if block_dir else self.t0002 / "blocknew"
        self.series_id = series_id

    # -- 路径 ---------------------------------------------------------------

    def extern_user_path(self) -> Path:
        return self.signals_dir / EXTERN_USER_FILE

    def series_dir(self, series_id: Optional[int] = None) -> Path:
        sid = self._require_series_id(series_id)
        return self.signals_dir / f"signals_user_{sid}"

    def series_dat_path(self, symbol: str, series_id: Optional[int] = None) -> Path:
        market, code = split_symbol(symbol)
        return self.series_dir(series_id) / f"{market}_{code}.dat"

    def block_path(self, block: str) -> Path:
        """block 可以是板块名（XG）也可以是完整路径。"""
        p = Path(block)
        if p.suffix.lower() == ".blk" and (p.is_absolute() or p.parent != Path(".")):
            return p
        return self.block_dir / f"{block}.blk"

    def _require_series_id(self, series_id: Optional[int]) -> int:
        sid = series_id if series_id is not None else self.series_id
        if sid is None:
            raise TdxBridgeError("未指定数据号：TdxBridge(tdx_dir, series_id=100) 或在调用时传 series_id")
        if not (1 <= int(sid) <= 999):
            raise TdxBridgeError(f"数据号 {sid} 越界：通达信自定义数据号通常为 1~999 的整数")
        return int(sid)

    # -- 通道 B：自定义序列数据（二进制 .dat） -------------------------------

    def write_user_series(self, symbol: str, series: Mapping[int, float] | Iterable[Tuple[int, float]],
                          *, series_id: Optional[int] = None) -> int:
        """整文件重写某只股票的序列数据。

        :param series: {YYYYMMDD: 数值} 或 [(日期, 数值)]，重复日期以最后一个为准。
        :return: 写入的记录条数（= K 线根数）

        幂等：同样输入两次调用产生完全相同的文件字节。
        注意数值按 float32 存储，有效精度约 7 位十进制，价格/信号类数据足够。
        """
        items: Dict[int, float] = {}
        for key, val in (series.items() if isinstance(series, Mapping) else series):
            items[normalize_date(key)] = float(val)
        if not items:
            raise TdxBridgeError("series 为空：没有任何记录可写（空文件会让通达信显示不出数据）")

        payload = bytearray()
        for day in sorted(items):
            try:
                payload += struct.pack("<if", int(day), float(items[day]))
            except struct.error as exc:
                raise TdxBridgeError(f"数值 {items[day]!r} 超出 float32 可表示范围") from exc

        path = self.series_dat_path(symbol, series_id)
        atomic_write_bytes(path, bytes(payload))
        return len(items)

    def read_user_series(self, symbol: str, *, series_id: Optional[int] = None) -> List[Tuple[int, float]]:
        """读回序列数据，用于写后校验。"""
        path = self.series_dat_path(symbol, series_id)
        if not path.is_file():
            raise TdxBridgeError(f"{path} 不存在")
        blob = path.read_bytes()
        if len(blob) % 8:
            raise TdxBridgeError(f"{path} 长度 {len(blob)} 不是 8 的整数倍，文件可能损坏")
        return [struct.unpack_from("<if", blob, off) for off in range(0, len(blob), 8)]

    def write_series_import_txt(self, path: os.PathLike | str,
                                records: Iterable[Tuple[str, int, float]]) -> int:
        """生成官方「自定义数据管理器 → 导入」用的 txt（最稳妥的通道）。

        :param records: [(合约代码, 日期, 数值)]，可混合多只股票
        行格式: 市场|代码|YYYYMMDD|数值
        """
        lines: List[str] = []
        for symbol, day, value in records:
            market, code = split_symbol(symbol)
            lines.append(f"{market}|{code}|{normalize_date(day)}|{format_value(value)}")
        if not lines:
            raise TdxBridgeError("records 为空")
        out = Path(path)
        atomic_write_bytes(out, text_to_gbk("\r\n".join(lines) + "\r\n", where="导入文件"))
        return len(lines)

    # -- 通道 A：外部数据（字符串 + 数值） -----------------------------------

    def read_extern_user(self) -> List[Tuple[int, str, int, str, str]]:
        """读回 extern_user.txt：[(市场, 代码, 数据号, 文字串, 数值串)]。"""
        path = self.extern_user_path()
        if not path.is_file():
            return []
        rows: List[Tuple[int, str, int, str, str]] = []
        for raw in path.read_bytes().decode(_GBK_ENCODING, errors="replace").splitlines():
            line = raw.strip()
            if not line:
                continue
            parts = line.split("|")
            if len(parts) < 5 or not parts[0].strip().isdigit():
                continue  # 非本格式的行原样保留在文件里，这里只是不解析
            rows.append((int(parts[0]), parts[1].strip(), int(parts[2]), parts[3], parts[4]))
        return rows

    def write_extern_user(self, rows: Iterable[Tuple[str, str, float, int]],
                          *, merge: bool = True) -> int:
        """写外部数据。

        :param rows:  [(合约代码, 文字串, 数值, 数据号)]
        :param merge: True（默认）保留文件里其它数据号的记录，只覆盖 ``rows`` 涉及的
                      (市场, 代码, 数据号) 组合；False 则整文件重写（会清掉别人写的数据，
                      除非你确定这个文件只有你自己在用）。
        :return: 写入条数

        公式端用 EXTERNSTR(0, 数据号) 取文字、EXTERNVALUE(0, 数据号) 取数值。
        文字串里不能出现 '|'（字段分隔符），出现会直接报错而不是写坏文件。
        """
        prepared: Dict[Tuple[int, str, int], Tuple[str, str]] = {}
        for symbol, text, value, data_id in rows:
            market, code = split_symbol(symbol)
            if "|" in str(text):
                raise TdxBridgeError(f"文字串不能包含 '|'：{text!r}")
            if "\n" in str(text) or "\r" in str(text):
                raise TdxBridgeError(f"文字串不能包含换行：{text!r}")
            prepared[(market, code, int(data_id))] = (str(text), format_value(value))

        if not prepared:
            raise TdxBridgeError("rows 为空")

        kept: List[str] = []
        if merge:
            for market, code, data_id, text, value in self.read_extern_user():
                if (market, code, data_id) not in prepared:
                    kept.append(f"{market}|{code}|{data_id}|{text}|{value}")

        written = [
            f"{market}|{code}|{data_id}|{text}|{value}"
            for (market, code, data_id), (text, value) in sorted(prepared.items())
        ]
        body = "\r\n".join(kept + written) + "\r\n"
        atomic_write_bytes(self.extern_user_path(), text_to_gbk(body, where="extern_user.txt"))
        return len(written)

    # -- 通道 C：自定义板块 / 自选股（.blk） ---------------------------------

    def write_block(self, block: str, symbols: Iterable[str], *, dedupe: bool = True) -> int:
        """整文件重写板块列表。

        :return: 写入条数
        注意：通达信要先在「自定义板块」里建好同名板块（写 blocknew.cfg 才是登记步骤，
        本模块不动 cfg），然后 Python 只重写 .blk 内容 —— 这是最不容易踩坑的做法。
        """
        seen: List[Tuple[int, str]] = []
        known = set()
        for symbol in symbols:
            market, code = split_symbol(symbol)
            key = (market, code)
            if dedupe and key in known:
                continue
            known.add(key)
            seen.append(key)
        if not seen:
            raise TdxBridgeError("symbols 为空：为避免误清空板块，写入空列表会被拒绝；如确需清空请手动处理 .blk 文件")
        body = "".join(f"{market}{code}\r\n" for market, code in sorted(seen))
        atomic_write_bytes(self.block_path(block), text_to_gbk(body, where="板块文件"))
        return len(seen)

    def read_block(self, block: str) -> List[Tuple[int, str]]:
        """读回板块列表 [(市场, 代码)]，用于写后校验。"""
        path = self.block_path(block)
        if not path.is_file():
            raise TdxBridgeError(f"{path} 不存在")
        rows: List[Tuple[int, str]] = []
        for raw in path.read_bytes().decode(_GBK_ENCODING, errors="replace").splitlines():
            line = raw.strip()
            if len(line) >= 7 and line[0].isdigit():
                rows.append((int(line[0]), line[1:7]))
            elif len(line) >= 6 and line[:6].isdigit():
                rows.append((split_symbol(line[:6])[0], line[:6]))
        return rows

    # -- 自检 ---------------------------------------------------------------

    def selftest(self, symbol: str = "SSE.600519") -> Dict[str, object]:
        """在临时目录里跑一遍读写的完整链路，确认本模块在你机器上的行为符合预期。"""
        with tempfile.TemporaryDirectory() as tmp:
            fake = Path(tmp) / "new_tdx"
            (fake / "T0002" / "signals").mkdir(parents=True)
            (fake / "T0002" / "blocknew").mkdir(parents=True)
            probe = TdxBridge(fake, series_id=100)
            probe.write_user_series(symbol, {20240902: 1.0, 20240903: 2.5})
            probe.write_extern_user([(symbol, "测试信号", 1.23, 100)])
            probe.write_block("XG", [symbol])
            return {
                "tdx_dir": str(fake),
                "series": probe.read_user_series(symbol),
                "extern": probe.read_extern_user(),
                "block": probe.read_block("XG"),
            }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tdx_bridge",
        description="把 Python/TqSdk 信号写进通达信（自定义数据 / 序列数据 / 板块）",
    )
    parser.add_argument("--tdx-dir", required=True, help=r"通达信安装目录，如 C:\new_tdx")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_series = sub.add_parser("series", help="CSV 导出为自定义序列数据 .dat（整文件重写）")
    p_series.add_argument("--csv", required=True, help="列: symbol,date,value")
    p_series.add_argument("--series-id", type=int, required=True)

    p_txt = sub.add_parser("series-txt", help="CSV 导出为官方导入用 txt")
    p_txt.add_argument("--csv", required=True)
    p_txt.add_argument("--out", required=True)

    p_ext = sub.add_parser("extern", help="CSV 导出为 extern_user.txt 外部数据")
    p_ext.add_argument("--csv", required=True, help="列: symbol,text,value,data_id")
    p_ext.add_argument("--no-merge", action="store_true", help="整文件重写（默认保留其它数据号）")

    p_block = sub.add_parser("block", help="把代码列表写成 .blk 板块文件")
    p_block.add_argument("--block", required=True, help="板块名，如 XG")
    p_block.add_argument("--symbols", required=True, help="逗号分隔，或每行一个的 txt 路径")

    sub.add_parser("selftest", help="临时目录里跑一遍读写自检")

    return parser


def _read_csv(path: str) -> List[List[str]]:
    import csv

    rows: List[List[str]] = []
    with open(path, newline="", encoding="utf-8-sig") as fh:
        reader = csv.reader(fh)
        for row in reader:
            if not row or not "".join(row).strip():
                continue
            if row[0].strip().lower() in ("symbol", "code", "代码", "合约"):
                continue  # 表头
            rows.append([c.strip() for c in row])
    return rows


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _build_parser().parse_args(argv)
    bridge = TdxBridge(args.tdx_dir, series_id=getattr(args, "series_id", None))

    try:
        if args.cmd == "selftest":
            print(bridge.selftest())
            return 0

        if args.cmd == "series":
            grouped: Dict[str, Dict[int, float]] = {}
            for symbol, day, value in _read_csv(args.csv):
                grouped.setdefault(symbol, {})[normalize_date(day)] = float(value)
            total = 0
            for symbol, series in grouped.items():
                count = bridge.write_user_series(symbol, series)
                back = bridge.read_user_series(symbol)
                assert len(back) == count, f"{symbol} 写后校验失败：写 {count} 条，读回 {len(back)} 条"
                print(f"[OK] {symbol}: {count} 条 → {bridge.series_dat_path(symbol)}")
                total += count
            print(f"共 {len(grouped)} 个品种 / {total} 条记录（已读回校验）")
            return 0

        if args.cmd == "series-txt":
            rows = [(s, normalize_date(d), float(v)) for s, d, v in _read_csv(args.csv)]
            count = bridge.write_series_import_txt(args.out, rows)
            print(f"[OK] {count} 行 → {args.out}（在通达信：公式系统 → 自定义数据管理器 → 导入）")
            return 0

        if args.cmd == "extern":
            rows = [(s, t, float(v), int(i)) for s, t, v, i in _read_csv(args.csv)]
            count = bridge.write_extern_user(rows, merge=not args.no_merge)
            print(f"[OK] {count} 条 → {bridge.extern_user_path()}")
            return 0

        if args.cmd == "block":
            src = Path(args.symbols)
            if src.is_file():
                symbols = [ln.strip() for ln in src.read_text(encoding="utf-8").splitlines() if ln.strip()]
            else:
                symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
            count = bridge.write_block(args.block, symbols)
            back = bridge.read_block(args.block)
            assert len(back) == count, f"板块写后校验失败：写 {count} 读回 {len(back)}"
            print(f"[OK] {count} 只 → {bridge.block_path(args.block)}（已读回校验）")
            return 0
    except TdxBridgeError as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 2

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
