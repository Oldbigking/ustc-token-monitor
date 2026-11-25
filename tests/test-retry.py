#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""采集重试逻辑的离线测试（不发网络请求，替换 fetch_rows）。

    python tests/test-retry.py
"""

import os
import sys
import time
import urllib.error

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import monitor  # noqa: E402

monitor.STATE["budget_ts"] = time.time() + 10_000   # 跳过 key/info 刷新


def case(name, failures, exc, expect_attempts, expect_err_contains):
    calls = {"n": 0}

    def fake(full=False):
        calls["n"] += 1
        if calls["n"] <= failures:
            raise exc
        return []

    orig = monitor.fetch_rows
    monitor.fetch_rows = fake
    monitor.STATE["last_err"] = None
    t0 = time.time()
    try:
        monitor.poll_once(verbose=False, force_full=False)
    finally:
        monitor.fetch_rows = orig
    dt = time.time() - t0
    err = monitor.STATE["last_err"]
    ok = calls["n"] == expect_attempts and ((err or "") .find(expect_err_contains) >= 0)
    print(f"{'PASS' if ok else 'FAIL'}  {name}: 尝试 {calls['n']} 次（期望 {expect_attempts}）"
          f"，{dt:.1f}s，last_err={err!r}")
    return ok


def main():
    e503 = urllib.error.HTTPError("u", 503, "Service Unavailable", {}, None)
    ok = True
    ok &= case("503 两次后恢复", 2, e503, 3, "")
    ok &= case("503 持续失败", 3, e503, 3, "平台返回 503（平台暂时不可用）")
    ok &= case("非临时错误不重试", 9, ValueError("boom"), 1, "ValueError: boom")
    print("\n全部通过" if ok else "\n存在失败")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
