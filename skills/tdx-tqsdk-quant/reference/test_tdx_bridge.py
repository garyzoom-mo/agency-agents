#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""tdx_bridge 的单元测试（纯标准库 unittest，无需通达信、无需网络）。

    python -m unittest -v skills/tdx-tqsdk-quant/reference/test_tdx_bridge.py
    # 或在该目录下：
    python -m unittest -v test_tdx_bridge
"""

from __future__ import annotations

import contextlib
import csv
import io
import struct
import sys
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from tdx_bridge import (  # noqa: E402
    MARKET_BJ,
    MARKET_SH,
    MARKET_SZ,
    TdxBridge,
    TdxBridgeError,
    UnsupportedSymbolError,
    format_value,
    main,
    normalize_date,
    split_symbol,
)


class TdxTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / "new_tdx"
        (self.root / "T0002" / "signals").mkdir(parents=True)
        (self.root / "T0002" / "blocknew").mkdir(parents=True)
        self.bridge = TdxBridge(self.root, series_id=100)

    def tearDown(self) -> None:
        self._tmp.cleanup()


class TestSymbolMapping(unittest.TestCase):
    def test_tqsdk_style(self):
        self.assertEqual(split_symbol("SSE.601456"), (MARKET_SH, "601456"))
        self.assertEqual(split_symbol("SZE.000001"), (MARKET_SZ, "000001"))
        self.assertEqual(split_symbol("BSE.430047"), (MARKET_BJ, "430047"))

    def test_wind_style_and_bare(self):
        self.assertEqual(split_symbol("601456.SH"), (MARKET_SH, "601456"))
        self.assertEqual(split_symbol("000001.SZ"), (MARKET_SZ, "000001"))
        self.assertEqual(split_symbol("600519"), (MARKET_SH, "600519"))
        self.assertEqual(split_symbol("300750"), (MARKET_SZ, "300750"))
        self.assertEqual(split_symbol("510300"), (MARKET_SH, "510300"))  # 沪深300ETF
        self.assertEqual(split_symbol("128036"), (MARKET_SZ, "128036"))  # 深市可转债

    def test_case_and_whitespace(self):
        self.assertEqual(split_symbol("  sse.600519 "), (MARKET_SH, "600519"))

    def test_futures_rejected_with_clear_message(self):
        with self.assertRaises(UnsupportedSymbolError) as ctx:
            split_symbol("SHFE.rb2510")
        self.assertIn("期货", str(ctx.exception))

    def test_empty_rejected(self):
        with self.assertRaises(UnsupportedSymbolError):
            split_symbol("")

    def test_unknown_exchange_rejected(self):
        with self.assertRaises(UnsupportedSymbolError):
            split_symbol("NYSE.AAPL")


class TestDateAndValue(unittest.TestCase):
    def test_normalize_date(self):
        self.assertEqual(normalize_date("2024-09-02"), 20240902)
        self.assertEqual(normalize_date("20240902"), 20240902)
        self.assertEqual(normalize_date(20240902), 20240902)
        self.assertEqual(normalize_date(date(2024, 9, 2)), 20240902)
        self.assertEqual(normalize_date(datetime(2024, 9, 2, 15, 0)), 20240902)

    def test_normalize_nanosecond_timestamp_from_tqsdk(self):
        # TqSdk 的 kline.datetime 是纳秒时间戳
        ns = int(datetime(2024, 9, 2, 15, 0).timestamp() * 1_000_000_000)
        self.assertEqual(normalize_date(ns), 20240902)

    def test_normalize_date_rejects_junk(self):
        with self.assertRaises(TdxBridgeError):
            normalize_date("昨天")
        with self.assertRaises(TdxBridgeError):
            normalize_date(True)

    def test_format_value_is_compact_and_not_scientific(self):
        self.assertEqual(format_value(1.0), "1")
        self.assertEqual(format_value(1.2300), "1.23")
        self.assertEqual(format_value(-0.0001), "-0.0001")
        self.assertNotIn("e", format_value(0.000001234))


class TestBridgeSetup(TdxTestBase):
    def test_missing_t0002_raises_actionable_error(self):
        TdxBridge(self.root)  # 正常：安装目录里就有 T0002
        with self.assertRaises(TdxBridgeError) as ctx:
            TdxBridge(self.root / "T0002")  # 误把 T0002 当安装目录
        self.assertIn("T0002", str(ctx.exception))

    def test_series_id_required(self):
        b = TdxBridge(self.root)
        with self.assertRaises(TdxBridgeError):
            b.write_user_series("SSE.600519", {20240902: 1.0})
        with self.assertRaises(TdxBridgeError):
            b.series_dir(0)


class TestUserSeries(TdxTestBase):
    def test_binary_layout_is_int32_date_plus_float32_value(self):
        bridge = TdxBridge(self.root, series_id=2)
        bridge.write_user_series("SZE.000516", {20190604: 21.34})
        blob = (self.root / "T0002" / "signals" / "signals_user_2" / "0_000516.dat").read_bytes()
        self.assertEqual(len(blob), 8)
        day, val = struct.unpack("<if", blob)
        self.assertEqual(day, 20190604)
        self.assertAlmostEqual(val, 21.34, places=5)

    def test_path_follows_market_prefix(self):
        path = self.bridge.series_dat_path("SSE.600519")
        self.assertEqual(path.name, "1_600519.dat")
        self.assertEqual(path.parent.name, "signals_user_100")

    def test_sorted_and_idempotent(self):
        path = self.bridge.series_dat_path("SSE.600519")
        self.bridge.write_user_series("SSE.600519", {20240903: 2.5, 20240902: 1.0})
        first = path.read_bytes()
        self.bridge.write_user_series("SSE.600519", {20240902: 1.0, 20240903: 2.5})
        self.assertEqual(first, path.read_bytes(), "同样输入必须产生同样字节（幂等）")
        days = [d for d, _ in self.bridge.read_user_series("SSE.600519")]
        self.assertEqual(days, [20240902, 20240903], "日期必须升序")

    def test_duplicate_date_last_wins(self):
        self.bridge.write_user_series("SSE.600519", [(20240902, 1.0), (20240902, 3.0)])
        self.assertEqual(self.bridge.read_user_series("SSE.600519"), [(20240902, 3.0)])

    def test_write_then_read_back(self):
        series = {"2024-09-02": 1.0, "2024-09-03": -1.0, "2024-09-04": 0.0}
        count = self.bridge.write_user_series("SSE.600519", series)
        self.assertEqual(count, 3)
        back = self.bridge.read_user_series("SSE.600519")
        self.assertEqual([d for d, _ in back], [20240902, 20240903, 20240904])
        self.assertEqual([v for _, v in back], [1.0, -1.0, 0.0])

    def test_empty_series_rejected(self):
        with self.assertRaises(TdxBridgeError):
            self.bridge.write_user_series("SSE.600519", {})

    def test_corrupt_file_detected(self):
        path = self.bridge.series_dat_path("SSE.600519")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"\x01\x02\x03")  # 3 字节，不是 8 的倍数
        with self.assertRaises(TdxBridgeError) as ctx:
            self.bridge.read_user_series("SSE.600519")
        self.assertIn("损坏", str(ctx.exception))

    def test_import_txt_format(self):
        out = Path(self._tmp.name) / "series.txt"
        count = self.bridge.write_series_import_txt(
            out, [("SSE.600519", "2024-09-02", 1.0), ("SZE.000001", 20240903, -0.5)]
        )
        self.assertEqual(count, 2)
        raw = out.read_bytes()
        self.assertTrue(raw.endswith(b"\r\n"))
        self.assertEqual(
            raw.decode("gbk").strip().split("\r\n"),
            ["1|600519|20240902|1", "0|000001|20240903|-0.5"],
        )


class TestExternUser(TdxTestBase):
    def test_merge_preserves_other_data_ids(self):
        path = self.bridge.extern_user_path()
        path.write_bytes("1|600519|8|别人写的|0.5\r\n".encode("gbk"))
        count = self.bridge.write_extern_user([("SSE.600519", "多头信号", 1.0, 100)])
        self.assertEqual(count, 1)
        rows = self.bridge.read_extern_user()
        self.assertIn((1, "600519", 8, "别人写的", "0.5"), rows)
        self.assertIn((1, "600519", 100, "多头信号", "1"), rows)

    def test_overwrite_same_symbol_and_id(self):
        self.bridge.write_extern_user([("SSE.600519", "旧", 1.0, 100)])
        self.bridge.write_extern_user([("SSE.600519", "新", 2.0, 100)])
        rows = self.bridge.read_extern_user()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][3], "新")

    def test_no_merge_wipes_file(self):
        self.bridge.write_extern_user([("SSE.600519", "A", 1.0, 100)])
        self.bridge.write_extern_user([("SZE.000001", "B", 2.0, 101)], merge=False)
        rows = self.bridge.read_extern_user()
        self.assertEqual(rows, [(0, "000001", 101, "B", "2")])

    def test_chinese_text_is_gbk_encoded(self):
        self.bridge.write_extern_user([("SSE.600519", "涨停板敢死队", 1.0, 100)])
        raw = self.bridge.extern_user_path().read_bytes()
        self.assertIn("涨停板敢死队".encode("gbk"), raw)

    def test_pipe_in_text_rejected(self):
        with self.assertRaises(TdxBridgeError):
            self.bridge.write_extern_user([("SSE.600519", "a|b", 1.0, 100)])

    def test_emoji_rejected_with_clear_message(self):
        with self.assertRaises(TdxBridgeError) as ctx:
            self.bridge.write_extern_user([("SSE.600519", "涨🚀", 1.0, 100)])
        self.assertIn("GBK", str(ctx.exception))

    def test_empty_rows_rejected(self):
        with self.assertRaises(TdxBridgeError):
            self.bridge.write_extern_user([])


class TestBlock(TdxTestBase):
    def test_blk_layout_7_bytes_per_line(self):
        self.bridge.write_block("XG", ["SSE.600519", "SZE.000001"])
        raw = self.bridge.block_path("XG").read_bytes()
        self.assertEqual(raw, b"0000001\r\n1600519\r\n")  # 按 (市场,代码) 排序，保证幂等

    def test_dedupe_and_roundtrip(self):
        count = self.bridge.write_block("XG", ["SSE.600519", "600519", "SZE.000001"])
        self.assertEqual(count, 2)
        self.assertEqual(self.bridge.read_block("XG"), [(0, "000001"), (1, "600519")])

    def test_empty_rejected(self):
        with self.assertRaises(TdxBridgeError):
            self.bridge.write_block("XG", [])

    def test_custom_path_with_suffix(self):
        custom = Path(self._tmp.name) / "my.blk"
        self.bridge.write_block(str(custom), ["SSE.600519"])
        self.assertTrue(custom.is_file())


class TestAtomicWrite(TdxTestBase):
    def test_no_temp_files_left_behind(self):
        self.bridge.write_user_series("SSE.600519", {20240902: 1.0})
        self.bridge.write_block("XG", ["SSE.600519"])
        self.bridge.write_extern_user([("SSE.600519", "x", 1.0, 100)])
        leftovers = [p for p in self.root.rglob("*.tmp")]
        self.assertEqual(leftovers, [], f"遗留临时文件: {leftovers}")

    def test_content_fully_replaced_not_truncated(self):
        self.bridge.write_user_series("SSE.600519", {20240902: 1.0, 20240903: 1.0})
        size_before = self.bridge.series_dat_path("SSE.600519").stat().st_size
        self.bridge.write_user_series("SSE.600519", {20240902: 1.0})
        size_after = self.bridge.series_dat_path("SSE.600519").stat().st_size
        self.assertEqual((size_before, size_after), (16, 8))


class TestCLI(TdxTestBase):
    """CLI 测试只看退出码与落盘结果，把 [OK] 之类的打印吞掉，保持测试输出干净。"""

    def _run(self, argv):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            return main(argv)

    def test_cli_series_end_to_end(self):
        csv_path = Path(self._tmp.name) / "signals.csv"
        with open(csv_path, "w", newline="", encoding="utf-8") as fh:
            writer = csv.writer(fh)
            writer.writerow(["symbol", "date", "value"])
            writer.writerow(["SSE.600519", "2024-09-02", "1"])
            writer.writerow(["SSE.600519", "2024-09-03", "-1"])
            writer.writerow(["SZE.000001", "2024-09-03", "0.5"])
        rc = self._run(["--tdx-dir", str(self.root), "series", "--csv", str(csv_path), "--series-id", "100"])
        self.assertEqual(rc, 0)
        self.assertEqual(len(self.bridge.read_user_series("SSE.600519")), 2)
        self.assertEqual(len(self.bridge.read_user_series("SZE.000001")), 1)

    def test_cli_block_from_txt(self):
        txt = Path(self._tmp.name) / "codes.txt"
        txt.write_text("600519\n000001\n", encoding="utf-8")
        rc = self._run(["--tdx-dir", str(self.root), "block", "--block", "XG", "--symbols", str(txt)])
        self.assertEqual(rc, 0)
        self.assertEqual(len(self.bridge.read_block("XG")), 2)

    def test_cli_error_exit_code(self):
        csv_path = Path(self._tmp.name) / "bad.csv"
        csv_path.write_text("symbol,date,value\nSHFE.rb2510,20240902,1\n", encoding="utf-8")
        rc = self._run(["--tdx-dir", str(self.root), "series", "--csv", str(csv_path), "--series-id", "100"])
        self.assertEqual(rc, 2, "非法代码应返回退出码 2，方便任务计划里判断失败")


class TestSelftest(TdxTestBase):
    def test_selftest_roundtrip(self):
        result = self.bridge.selftest("SZE.300750")
        self.assertEqual(result["series"], [(20240902, 1.0), (20240903, 2.5)])
        self.assertEqual(result["block"], [(0, "300750")])
        self.assertEqual(result["extern"][0][3], "测试信号")


if __name__ == "__main__":
    unittest.main(verbosity=2)
