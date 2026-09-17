#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""example_strategy 的离线测试：策略逻辑 + 未来函数自检 + 端到端导出。

    python -m unittest -v test_example_strategy
"""

from __future__ import annotations

import contextlib
import io
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import example_strategy as ex  # noqa: E402
from tdx_bridge import TdxBridge  # noqa: E402


def leaky_compute(bars):
    """故意作弊的策略：用全样本最大值归一化 —— 典型的未来函数，自检必须抓到。"""
    closes = [c for _, c in bars]
    peak = max(closes) if closes else 1.0
    return {d: (1.0 if c >= peak else 0.0) for d, c in bars}


class TestStrategyLogic(unittest.TestCase):
    def test_signals_are_binary_and_dated(self):
        bars = ex.load_bars_demo("SSE.600519")
        signals = ex.compute_position_signals(bars)
        self.assertTrue(signals)
        self.assertTrue(set(signals.values()) <= {0.0, 1.0})
        self.assertEqual(sorted(signals), list(signals), "信号的日期必须升序")
        self.assertTrue(set(signals) <= {d for d, _ in bars}, "不得凭空造出非交易日")

    def test_insufficient_history_returns_empty(self):
        bars = ex.load_bars_demo("SSE.600519")[:10]
        self.assertEqual(ex.compute_position_signals(bars, fast=5, slow=20), {})

    def test_deterministic(self):
        bars = ex.load_bars_demo("SZE.300750")
        self.assertEqual(ex.compute_position_signals(bars), ex.compute_position_signals(bars))

    def test_high_vol_filter_can_shut_down_entries(self):
        bars = ex.load_bars_demo("SSE.600519")
        loose = ex.compute_position_signals(bars, vol_max=1.0)
        tight = ex.compute_position_signals(bars, vol_max=0.0001)
        self.assertGreaterEqual(sum(loose.values()), sum(tight.values()))

    def test_action_text_transitions(self):
        self.assertEqual(ex.action_text(1.0, None), "开多")
        self.assertEqual(ex.action_text(1.0, 0.0), "开多")
        self.assertEqual(ex.action_text(1.0, 1.0), "持有")
        self.assertEqual(ex.action_text(0.0, 1.0), "平仓")
        self.assertEqual(ex.action_text(0.0, 0.0), "空仓")


class TestLookaheadGuard(unittest.TestCase):
    def test_clean_strategy_passes(self):
        bars = ex.load_bars_demo("SSE.601318")
        checked = ex.assert_prefix_stable(bars)
        self.assertGreater(checked, 100)

    def test_leaky_strategy_is_caught(self):
        bars = ex.load_bars_demo("SSE.601318")
        with self.assertRaises(AssertionError) as ctx:
            ex.assert_prefix_stable(bars, leaky_compute)
        self.assertIn("未来函数", str(ctx.exception))

    def test_insufficient_sample_refuses_to_claim_pass(self):
        bars = ex.load_bars_demo("SSE.601318")[:30]
        with self.assertRaises(AssertionError):
            ex.assert_prefix_stable(bars)


class TestEndToEnd(unittest.TestCase):
    def _silent(self, argv):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            code = ex.main(argv)
        return code, buf.getvalue()

    def test_demo_writes_all_three_channels(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "new_tdx"
            (root / "T0002" / "signals").mkdir(parents=True)
            (root / "T0002" / "blocknew").mkdir(parents=True)
            code, out = self._silent(["demo", "--tdx-dir", str(root), "--symbols", "SSE.600519,SZE.300750"])
            self.assertEqual(code, 0, out)
            bridge = TdxBridge(root, series_id=ex.SERIES_ID_POSITION)
            self.assertGreater(len(bridge.read_user_series("SSE.600519")), 60)
            self.assertTrue(bridge.extern_user_path().is_file())
            self.assertTrue(bridge.block_path(ex.BLOCK_NAME).exists())

    def test_verify_reports_fresh_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "new_tdx"
            (root / "T0002" / "signals").mkdir(parents=True)
            (root / "T0002" / "blocknew").mkdir(parents=True)
            self._silent(["demo", "--tdx-dir", str(root), "--symbols", "SSE.600519"])
            code, out = self._silent(["verify", "--tdx-dir", str(root), "--symbols", "SSE.600519"])
            self.assertEqual(code, 0, out)
            self.assertIn("[OK]", out)

    def test_dry_run_does_not_touch_disk(self):
        code, out = self._silent(["demo", "--tdx-dir", "/nonexistent/tdx", "--dry-run"])
        self.assertEqual(code, 0, out)
        self.assertIn("未写入任何文件", out)
        self.assertFalse(Path("/nonexistent/tdx").exists())

    def test_missing_tdx_dir_for_export_is_rejected(self):
        code, out = self._silent(["export", "--symbols", "SSE.600519"])
        self.assertEqual(code, 2)
        self.assertIn("--tdx-dir", out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
