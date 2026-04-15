#!/usr/bin/env python3
import argparse
import json
import os
import re
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Sequence, Tuple


@dataclass(frozen=True)
class Window:
    start: int  # inclusive, 0-based
    end: int  # inclusive, 0-based


def _parse_names(raw: Optional[Sequence[str]], names_csv: Optional[str]) -> List[str]:
    names: List[str] = []
    if raw:
        for n in raw:
            if n:
                names.append(n)
    if names_csv:
        for part in names_csv.split(","):
            p = part.strip()
            if p:
                names.append(p)
    # stable unique, keep order
    seen = set()
    out = []
    for n in names:
        if n not in seen:
            seen.add(n)
            out.append(n)
    return out


def _compile_terms(terms: Sequence[str]) -> List[Tuple[str, re.Pattern]]:
    compiled: List[Tuple[str, re.Pattern]] = []
    for t in terms:
        # literal match; we want simple "contains" semantics but also need matched terms list
        compiled.append((t, re.compile(re.escape(t))))
    return compiled


def _line_has_quote(line: str) -> bool:
    # Common CJK quotes + ascii quotes
    return any(q in line for q in ["“", "”", "\"", "『", "』", "「", "」", "《", "》"])


def _find_hits(
    lines: Sequence[str],
    term_patterns: Sequence[Tuple[str, re.Pattern]],
    require_quote: bool,
    exclude_terms: Sequence[str],
) -> List[Tuple[int, List[str]]]:
    excludes = [re.compile(re.escape(t)) for t in exclude_terms if t]
    hits: List[Tuple[int, List[str]]] = []
    for i, line in enumerate(lines):
        if require_quote and not _line_has_quote(line):
            continue
        if excludes and any(p.search(line) for p in excludes):
            continue
        matched = [t for (t, p) in term_patterns if p.search(line)]
        if matched:
            hits.append((i, matched))
    return hits


def _make_windows(hits: Sequence[int], n_lines: int, context: int) -> List[Window]:
    windows = []
    for i in hits:
        s = max(0, i - context)
        e = min(n_lines - 1, i + context)
        windows.append(Window(s, e))
    return windows


def _merge_windows(windows: Sequence[Window]) -> List[Window]:
    if not windows:
        return []
    ws = sorted(windows, key=lambda w: (w.start, w.end))
    merged: List[Window] = []
    cur = ws[0]
    for w in ws[1:]:
        if w.start <= cur.end + 1:
            cur = Window(cur.start, max(cur.end, w.end))
        else:
            merged.append(cur)
            cur = w
    merged.append(cur)
    return merged


def _ensure_min_window(w: Window, n_lines: int, min_lines: int) -> Window:
    if min_lines <= 0:
        return w
    cur_len = w.end - w.start + 1
    if cur_len >= min_lines:
        return w
    need = min_lines - cur_len
    expand_left = need // 2
    expand_right = need - expand_left
    s = max(0, w.start - expand_left)
    e = min(n_lines - 1, w.end + expand_right)
    # if clamped, try expand on the other side
    cur_len2 = e - s + 1
    if cur_len2 < min_lines:
        # expand further where possible
        s = max(0, s - (min_lines - cur_len2))
        e = min(n_lines - 1, e + (min_lines - cur_len2))
    return Window(s, e)


def _collect_matched_terms_for_window(
    window: Window, hit_map: Dict[int, List[str]]
) -> List[str]:
    terms: List[str] = []
    seen = set()
    for i in range(window.start, window.end + 1):
        for t in hit_map.get(i, []):
            if t not in seen:
                seen.add(t)
                terms.append(t)
    return terms


def _read_text_lines(path: str) -> List[str]:
    # Prefer utf-8; fallback to gb18030 for common CN novel dumps.
    for enc in ("utf-8", "utf-8-sig", "gb18030"):
        try:
            with open(path, "r", encoding=enc, errors="strict") as f:
                return [ln.rstrip("\n") for ln in f.readlines()]
        except UnicodeDecodeError:
            continue
    # last resort: replace errors
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        return [ln.rstrip("\n") for ln in f.readlines()]


def _write_markdown(
    output_path: str,
    input_path: str,
    names: Sequence[str],
    context_lines: int,
    require_quote: bool,
    exclude_terms: Sequence[str],
    windows: Sequence[Window],
    lines: Sequence[str],
    hit_map: Dict[int, List[str]],
) -> None:
    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        f.write("# 角色相关片段（自动抽取）\n\n")
        f.write("> 由 `scripts/extract_character_snippets.py` 生成。\n")
        f.write("> 建议下一步：将本文件片段喂给 `extract-fiction-character` 提取档案，再用 `fiction-persona` 编译系统提示词。\n\n")
        f.write("## Meta\n\n")
        f.write(f"- input: `{input_path}`\n")
        f.write(f"- names: {', '.join(f'`{n}`' for n in names)}\n")
        f.write(f"- context_lines: {context_lines}\n")
        f.write(f"- require_quote: {str(require_quote).lower()}\n")
        if exclude_terms:
            f.write(f"- exclude_terms: {', '.join(f'`{t}`' for t in exclude_terms)}\n")
        f.write(f"- windows: {len(windows)}\n\n")
        for idx, w in enumerate(windows, start=1):
            terms = _collect_matched_terms_for_window(w, hit_map)
            f.write(f"\n--- 片段 {idx} (行 {w.start + 1}-{w.end + 1}) ---\n")
            if terms:
                f.write(f"matched_terms: {', '.join(terms)}\n")
            for i in range(w.start, w.end + 1):
                f.write(lines[i] + "\n")


def _write_jsonl(
    output_path: str,
    input_path: str,
    names: Sequence[str],
    context_lines: int,
    require_quote: bool,
    exclude_terms: Sequence[str],
    windows: Sequence[Window],
    lines: Sequence[str],
    hit_map: Dict[int, List[str]],
) -> None:
    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        for idx, w in enumerate(windows, start=1):
            obj = {
                "snippet_id": idx,
                "input": input_path,
                "names": list(names),
                "context_lines": context_lines,
                "require_quote": require_quote,
                "exclude_terms": list(exclude_terms),
                "line_start": w.start + 1,  # 1-based for humans
                "line_end": w.end + 1,
                "matched_terms": _collect_matched_terms_for_window(w, hit_map),
                "text": "\n".join(lines[w.start : w.end + 1]),
            }
            f.write(json.dumps(obj, ensure_ascii=False) + "\n")


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description="Extract all character-related snippets from a novel text file by names, with context windows and merging."
    )
    ap.add_argument("--input", required=True, help="Path to novel text file (single file).")
    ap.add_argument(
        "--name",
        action="append",
        default=[],
        help="Character name/alias term (repeatable).",
    )
    ap.add_argument(
        "--names",
        default="",
        help="Comma-separated character name/alias terms.",
    )
    ap.add_argument(
        "--context-lines",
        type=int,
        default=20,
        help="Number of lines before/after each hit to include.",
    )
    ap.add_argument(
        "--min-window-lines",
        type=int,
        default=0,
        help="Ensure each merged window has at least this many lines (0 disables).",
    )
    ap.add_argument(
        "--require-quote",
        action="store_true",
        help="Only count hits on lines that contain quote-like characters (for dialogue-first extraction).",
    )
    ap.add_argument(
        "--exclude",
        action="append",
        default=[],
        help="Exclude lines containing this term (repeatable).",
    )
    ap.add_argument(
        "--output",
        required=True,
        help="Output file path. Use .md for Markdown or .jsonl for JSONL.",
    )
    args = ap.parse_args(argv)

    names = _parse_names(args.name, args.names)
    if not names:
        raise SystemExit("No names provided. Use --name or --names.")
    if args.context_lines < 0:
        raise SystemExit("--context-lines must be >= 0")
    if args.min_window_lines < 0:
        raise SystemExit("--min-window-lines must be >= 0")

    lines = _read_text_lines(args.input)
    term_patterns = _compile_terms(names)
    hits = _find_hits(
        lines=lines,
        term_patterns=term_patterns,
        require_quote=args.require_quote,
        exclude_terms=args.exclude,
    )
    hit_lines = [i for (i, _) in hits]
    hit_map: Dict[int, List[str]] = {i: ts for (i, ts) in hits}

    windows = _make_windows(hit_lines, len(lines), args.context_lines)
    merged = _merge_windows(windows)
    merged = [_ensure_min_window(w, len(lines), args.min_window_lines) for w in merged]

    out_lower = args.output.lower()
    if out_lower.endswith(".jsonl"):
        _write_jsonl(
            output_path=args.output,
            input_path=args.input,
            names=names,
            context_lines=args.context_lines,
            require_quote=args.require_quote,
            exclude_terms=args.exclude,
            windows=merged,
            lines=lines,
            hit_map=hit_map,
        )
    else:
        _write_markdown(
            output_path=args.output,
            input_path=args.input,
            names=names,
            context_lines=args.context_lines,
            require_quote=args.require_quote,
            exclude_terms=args.exclude,
            windows=merged,
            lines=lines,
            hit_map=hit_map,
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

