#!/usr/bin/env python3
"""docs/prices.json の単価を LiteLLM の新しいコミットへ追従させる（2026-09-23 の更新と同じ規則）。

規則:
  1. 出所の追跡 — 各要素を、基準コミット（前回の更新に使った LiteLLM）で単価 5 欄が
     完全一致した項目へ結び付ける（鍵そのものがあればそれだけ、無ければ "/" で区切った
     末尾が鍵と一致する項目）。結び付いた項目が新しいコミットで変わり、変わった先が
     1 通りに定まるときだけ単価を書き換える。結び付かない要素・割れた要素は触らない
  2. 手写しの要素は触らない — 鍵に "/" を含むもの（Perplexity 経由）と HAND_KEYS
  3. 新規 — 基準コミットに無く新しいコミットに現れたモデル（末尾の名前で数える）。
     mode が chat / responses だけ・入力と出力の単価がある・候補の単価 5 欄が一致する・
     $1,000/MTok 以下、のときだけ足す。max_input_tokens は build_prices.py と同じ規則
  4. fetched と _notice を更新し、元の書式（indent=1・ensure_ascii=False・末尾改行・
     鍵の昇順）で書き戻す

使い方:
  python -X utf8 tools/refresh_prices.py --base 721d39f --dry-run
  python -X utf8 tools/refresh_prices.py --base 721d39f [--head <sha>]

既存要素の max_input_tokens は触らない（窓の更新は build_prices.py の役目）。
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import subprocess
import sys
import urllib.request
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from build_prices import dump, resolve  # noqa: E402

PRICES = Path(__file__).resolve().parent.parent / "docs" / "prices.json"
RAW = "https://raw.githubusercontent.com/BerriAI/litellm/{sha}/model_prices_and_context_window.json"
MAP = [
    ("input_cost_per_token", "input_per_mtok"),
    ("output_cost_per_token", "output_per_mtok"),
    ("cache_read_input_token_cost", "cache_read_per_mtok"),
    ("cache_creation_input_token_cost", "cache_write_per_mtok"),
    ("cache_creation_input_token_cost_above_1hr", "cache_write_1h_per_mtok"),
]
RATE_FIELDS = [b for _, b in MAP]
# 手で書き写した要素（鍵に "/" を含むものは別に除外する）。
HAND_KEYS = {
    "claude-fable-5-1", "gemini-3.8-flash", "muse-spark-1.3", "muse-spark-1.3-contributor",
    "gpt-6-astra", "gpt-6-luna", "gpt-6-sol", "grok-4.7",
}
MODES = {"chat", "responses"}
SANITY = 1000.0


def full_sha(short: str) -> tuple[str, str]:
    out = subprocess.run(
        ["gh", "api", f"repos/BerriAI/litellm/commits/{short}", "--jq", '.sha + " " + .commit.committer.date'],
        check=True, capture_output=True, text=True,
    ).stdout.split()
    return out[0], out[1]


def fetch(sha: str) -> dict:
    with urllib.request.urlopen(RAW.format(sha=sha), timeout=120) as r:
        d = json.load(r)
    d.pop("sample_spec", None)
    return {k: v for k, v in d.items() if isinstance(v, dict)}


def conv(item: dict) -> dict:
    d = {}
    for a, b in MAP:
        x = item.get(a)
        if isinstance(x, (int, float)) and not isinstance(x, bool):
            d[b] = round(x * 1e6, 10)
    return d


def tup(d: dict) -> tuple:
    return tuple(d.get(f) for f in RATE_FIELDS)


def tails(lit: dict) -> dict[str, list[str]]:
    t: dict[str, list[str]] = defaultdict(list)
    for k in lit:
        t[k.rsplit("/", 1)[-1]].append(k)
    return t


def sane(d: dict) -> bool:
    return all(v <= SANITY for v in d.values())


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", required=True)
    ap.add_argument("--head", default="main")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)

    raw = PRICES.read_bytes().replace(b"\r\n", b"\n")
    table = json.loads(raw)
    if dump(table) != raw:
        print("書式の自己検査に失敗: 無改変の再シリアライズが元と一致しない。書かない", file=sys.stderr)
        return 2

    base_sha, _ = full_sha(a.base)
    head_sha, head_date = full_sha(a.head)
    base, head = fetch(base_sha), fetch(head_sha)
    tb, th = tails(base), tails(head)

    counts: dict[str, int] = defaultdict(int)
    changed: list[str] = []
    models = []
    for m in table["models"]:
        k = m["key"]
        if "/" in k or k in HAND_KEYS:
            counts["hand"] += 1
            models.append(m)
            continue
        src = [k] if k in base else tb.get(k, [])
        matched = [s for s in src if tup(conv(base[s])) == tup(m)]
        if not matched:
            counts["untraced"] += 1
            models.append(m)
            continue
        if any(s not in head for s in matched):
            counts["source-gone"] += 1
            models.append(m)
            continue
        news = {tup(conv(head[s])) for s in matched}
        if len(news) != 1:
            counts["disagree"] += 1
            models.append(m)
            continue
        new = conv(head[matched[0]])
        # 同じ名前の別の項目が今も古い単価のままなら、どちらが正しいか決められない
        # （2026-10-06: openrouter の deepseek-v4.1-flash だけが入力 0.3 → 0.003 に化け、
        # azure_ai の同名は 0.3 のままだった）。割れたら触らない、に含める
        hsrc = [k] if k in head else th.get(k, [])
        if tup(new) != tup(m) and any(tup(conv(head[s])) == tup(m) for s in hsrc if s not in matched):
            counts["disagree-with-sibling"] += 1
            models.append(m)
            continue
        if tup(new) == tup(m):
            counts["same"] += 1
            models.append(m)
            continue
        if not sane(new) or "input_per_mtok" not in new or "output_per_mtok" not in new:
            counts["rejected"] += 1
            models.append(m)
            continue
        entry = {"key": k}
        if "max_input_tokens" in m:
            entry["max_input_tokens"] = m["max_input_tokens"]
        entry.update({f: new[f] for f in RATE_FIELDS if f in new})
        models.append(entry)
        changed.append(f"{k}: {tup(m)} -> {tup(new)}")
        counts["updated"] += 1

    have = {m["key"] for m in models}
    # build_prices.resolve は「"/" を含む鍵の末尾 → 項目の本体」の表を受け取る
    by_tail: dict[str, list[dict]] = defaultdict(list)
    for hk, hv in head.items():
        if "/" in hk:
            by_tail[hk.rsplit("/", 1)[-1]].append(hv)
    added: list[str] = []
    for t in sorted(set(th) - set(tb)):
        if t in have:
            counts["new-already-present"] += 1
            continue
        src = [t] if t in head else th[t]
        items = [head[s] for s in src]
        if not all(i.get("mode") in MODES for i in items):
            counts["new-mode"] += 1
            continue
        rates = {tup(conv(i)) for i in items}
        d = conv(items[0])
        if len(rates) != 1 or "input_per_mtok" not in d or "output_per_mtok" not in d:
            counts["new-norate-or-disagree"] += 1
            continue
        if not sane(d):
            counts["new-insane"] += 1
            continue
        entry = {"key": t}
        w, _ = resolve(t, head, by_tail)
        if w is not None:
            entry["max_input_tokens"] = w
        entry.update({f: d[f] for f in RATE_FIELDS if f in d})
        models.append(entry)
        added.append(t)
        counts["added"] += 1

    models.sort(key=lambda m: m["key"])
    table["models"] = models
    today = _dt.date.today().isoformat()
    table["fetched"] = today
    table["_notice"] += (
        f" On {today} rates were refreshed from LiteLLM commit {head_sha[:7]} the same way"
        f" (traced from commit {base_sha[:7]}): {counts['updated']} entries updated and"
        f" {counts['added']} models added; hand-transcribed entries were not touched."
    )

    print(f"base={base_sha[:7]} head={head_sha[:7]} ({head_date})")
    print(" ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    print(f"entries {len(models) - counts['added']} -> {len(models)}")
    for c in changed:
        print("  ~", c)
    for t in added:
        print("  +", t)
    if a.dry_run:
        return 0
    PRICES.write_bytes(dump(table))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
