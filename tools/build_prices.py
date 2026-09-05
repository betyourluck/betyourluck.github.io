#!/usr/bin/env python3
"""docs/prices.json に max_input_tokens を足す（Fuseforks Spec 50 P0。加算のみ）。

やること:
  1. 既存の docs/prices.json を読む（単価・鍵・fetched は 1 桁も変えない）
  2. BerriAI/litellm の model_prices_and_context_window.json を取る
  3. Spec 50 D2 の規則で各要素へ max_input_tokens を足す
       (1) LiteLLM に鍵そのものがあれば、その要素の max_input_tokens だけを見る。
           正の整数なら採る。無いか 0 なら「窓なし」で確定し (2) へは落ちない
       (2) 無ければ "/" で区切った末尾が鍵と完全一致する候補を集め、0 と欠落を除いた
           max_input_tokens が 1 種類に定まるときだけ採る
       (3) 割れたら書かない
  4. _notice に 1 文足す
  5. 元の書式（indent=1・ensure_ascii=False・末尾改行）で書き戻す

やらないこと: 単価の更新（利用者裁定「固定して取り込む」）。要素の追加・削除。

使い方:
  python -X utf8 tools/build_prices.py            # 書き戻す
  python -X utf8 tools/build_prices.py --dry-run  # 件数だけ出す

書式の自己検査: 読んだ表を無改変で再シリアライズしたものが元のバイト列と一致しない
場合は何も書かずに落ちる（書式が変わると git diff に単価以外の差分が混ざり、
「単価が動いていない」が diff から読めなくなる）。
"""

from __future__ import annotations

import datetime as _dt
import json
import sys
import urllib.request
from collections import defaultdict
from pathlib import Path

LITELLM_URL = (
    "https://raw.githubusercontent.com/BerriAI/litellm/main/"
    "model_prices_and_context_window.json"
)
PRICES = Path(__file__).resolve().parent.parent / "docs" / "prices.json"
U32_MAX = 2**32 - 1
FIELD = "max_input_tokens"


def dump(table: dict) -> bytes:
    """元の書式。`indent=1` / `ensure_ascii=False` / 末尾改行 1 つ。"""
    return (json.dumps(table, indent=1, ensure_ascii=False) + "\n").encode("utf-8")


def positive_window(entry: dict) -> int | None:
    """LiteLLM の 1 要素から、採ってよい窓だけを返す（正の整数で u32 に収まるもの）。"""
    v = entry.get(FIELD)
    if isinstance(v, bool) or not isinstance(v, int):
        return None
    if v <= 0 or v > U32_MAX:
        return None
    return v


def resolve(key: str, litellm: dict, by_tail: dict[str, list]) -> tuple[int | None, str]:
    """D2 の 3 段。戻りは (窓, 判定の名前)。"""
    bare = litellm.get(key)
    if isinstance(bare, dict):
        w = positive_window(bare)
        return (w, "bare") if w is not None else (None, "bare-none")
    cands = {positive_window(e) for e in by_tail.get(key, [])}
    cands.discard(None)
    if not cands:
        return None, "no-candidate"
    if len(cands) == 1:
        return next(iter(cands)), "tail-agree"
    return None, "tail-disagree"


def main(argv: list[str]) -> int:
    dry = "--dry-run" in argv
    # git の autocrlf が働く端末では checkout 直後の作業コピーが CRLF になる（コミットされる
    # blob は LF）。比較も書き戻しも LF で行い、改行の差を「書式の変化」と読まない。
    raw = PRICES.read_bytes().replace(b"\r\n", b"\n")
    table = json.loads(raw)
    if dump(table) != raw:
        print("書式の自己検査に失敗: 無改変の再シリアライズが元と一致しない。書かない", file=sys.stderr)
        return 2

    with urllib.request.urlopen(LITELLM_URL, timeout=120) as r:
        litellm = json.load(r)
    by_tail: dict[str, list] = defaultdict(list)
    for k, v in litellm.items():
        if isinstance(v, dict) and "/" in k:
            by_tail[k.rsplit("/", 1)[-1]].append(v)

    counts: dict[str, int] = defaultdict(int)
    rebuilt = []
    for m in table["models"]:
        w, why = resolve(m["key"], litellm, by_tail)
        counts[why] += 1
        # 欄は "key" の直後に置く。末尾に足すと、その前の行（元の最後の欄）に
        # カンマが付いて diff が 1 要素あたり「1 行変更 + 1 行追加」になり、
        # 「単価が動いていない」を追加行だけで読めなくなる（2026-09-05 に実際に踏んだ）。
        # "key" の直後なら diff は純粋な 1 行追加。
        rest = {k: v for k, v in m.items() if k not in ("key", FIELD)}
        entry = {"key": m["key"]}
        if w is not None:
            entry[FIELD] = w
        entry.update(rest)
        rebuilt.append(entry)
    table["models"] = rebuilt

    filled = sum(1 for m in table["models"] if FIELD in m)
    today = _dt.date.today().isoformat()
    sentence = (
        f" {FIELD} was added on {today} from the same LiteLLM file as of that day"
        f" (bare key first; prefixed candidates only when they agree; omitted when they"
        f" disagree or are absent) — rates and keys were left untouched."
    )
    notice = table["_notice"]
    marker = f" {FIELD} was added on "
    if marker in notice:
        notice = notice[: notice.index(marker)]
    table["_notice"] = notice + sentence

    print(f"entries={len(table['models'])} filled={filled} " + " ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    if dry:
        return 0
    PRICES.write_bytes(dump(table))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
