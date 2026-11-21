#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""timing 段的**唯一**解析（1.1 只剩两项投递配置）。

1.1 读 `config.json` 的 `timing` 段只为一件事：Core 建轮时那个批窗
（`normal_window`）与一轮最多带几封正式信（`max_formal_batch`）。文件读取归调用方
（它才知道本次的权威根），这里只有**纯计算**：给一个 `timing` 段，返回确定的值。

三条不变量，删掉任何一条都会让两个读者对同一份配置说不同的话：

* **段缺失 ⇒ 代码默认**，并回报哪些键是默认来的（`legacy_keys`）——"没配"是常态，不是错误；
* **类型不对 / 拼写错 ⇒ 报无效**，不静默回落（静默回落正是"现场配了 5 秒、跑起来 90 秒"的来源）；
* **真退役过的键 ⇒ 忽略并回报**，不判整份无效（旧部署的 `config.json` 里还留着它们）。

默认值只有这一份：调用方不得再写一遍 `90` / `20`。
"""

#: 唯一的代码默认（键集就是白名单）。
DEFAULT_TIMING = {
    "normal_window": 90,      # 普通信批窗（秒）
    "max_formal_batch": 20,   # 一轮正式信最多带几封
}

#: 可配的 timing 键。白名单之外（且不在退役表里）的键一律报无效。
TIMING_KEYS = tuple(DEFAULT_TIMING)

#: **真实退役过**的键：旧配置里可能还留着，忽略并回报，不判整份无效。
RETIRED_TIMING_KEYS = ("grace", "grace_idle", "grace_urgent", "rate_n", "rate_win")


class PolicySnapshotError(RuntimeError):
    """timing 段显式错误：类型不对 / 未知键 / 不是对象。绝不静默回落默认值。"""

    def __init__(self, reason, path="", detail=""):
        self.reason = reason
        self.path = str(path or "")
        self.detail = detail
        super().__init__("policy snapshot error: %s (%s)%s" % (
            reason, self.path, (" - " + detail) if detail else ""))


def resolve_timing(timing_section, *, defaults=None):
    """把 timing 段解析成完整配置值、默认来的键与已退役键。纯函数、显式入参。

    返回值是 `(values, legacy_keys, retired_keys)`。
    """
    defaults = dict(DEFAULT_TIMING if defaults is None else defaults)
    if timing_section is None:
        return ({k: defaults.get(k) for k in TIMING_KEYS}, list(TIMING_KEYS), [])
    if not isinstance(timing_section, dict):
        raise PolicySnapshotError("invalid_timing", "", "timing is not an object")
    retired_keys = sorted(set(timing_section) & set(RETIRED_TIMING_KEYS))
    unknown = sorted(set(timing_section) - set(TIMING_KEYS) - set(RETIRED_TIMING_KEYS))
    if unknown:
        raise PolicySnapshotError("invalid_timing", "",
                                  "unknown timing keys: %s (known: %s; retired: %s)"
                                  % (unknown, list(TIMING_KEYS), list(RETIRED_TIMING_KEYS)))
    values, legacy_keys = {}, []
    for key in TIMING_KEYS:
        if key not in timing_section:
            values[key] = defaults.get(key)
            legacy_keys.append(key)
            continue
        raw = timing_section[key]
        if isinstance(raw, bool) or not isinstance(raw, int) or raw < 0:
            raise PolicySnapshotError("invalid_timing", "",
                                      "timing.%s must be a non-negative int, got %r" % (key, raw))
        values[key] = raw
    return values, legacy_keys, retired_keys
