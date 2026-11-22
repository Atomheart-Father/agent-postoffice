"""R package: controlled artifact read. Core decides WHETHER (artifact.read.authorize); this module decides HOW.

One source: a submitted artifact -- the exact bytes snapshotted at publish time under their digest (never the
mutable working copy). Loading those bytes and verifying their digest live in ONE place,
`lib/work_result.read_version` (what `work.read` runs); this module only renders them.

Views (all bounded; a partial answer says so and how to continue):
  lines=[start,end] (1-based, inclusive) · search="text" · compare_data=<other version bytes> (unified diff,
  itself paged with cursor/limit/has_more over diff lines: a long diff is read the same way as text)
  cursor=<line number or returned long-line continuation>. Text is UTF-8; anything else is reported as binary with its size -- never decoded
  as if it were text, never claimed to be previewable."""
import difflib, re

DEFAULT_LINES = 200
MAX_LINES = 2000
MAX_BYTES = 128 * 1024                 # per answer, not per object
MAX_MATCHES = 100
_DIGEST = re.compile(r"sha256:([0-9a-f]{64})")


class ArtifactReadRefused(Exception):
    """Refusal with both wordings; the host picks one by the reader's session language (`text`)."""
    def __init__(self, code, message, en=None):
        super().__init__(message); self.code = code; self.message = message; self.message_en = en

    def text(self, locale="zh"):
        return self.message_en if locale == "en" and self.message_en else self.message


def digest_name(exact_version):
    m = _DIGEST.fullmatch(exact_version) if isinstance(exact_version, str) else None
    if not m:
        raise ArtifactReadRefused("invalid_request", "exact_version 必须是 sha256:<64位小写十六进制>", en="exact_version must be sha256:<64 lowercase hex>")
    return m.group(1)


def version_name(digest_hex):
    """`<64 hex>` → `sha256:<64 hex>`：`digest_name` 的**反方向**，两个方向挨着放、各写一次。

    这个令牌是线路契约：`publish` 产出它、`digest_name` 解析它、每一处把版本给读的人看的地方
    都重复它。散着写就总有一处能漂成 `digest_name` 拒收的样子 —— 那一版交上去、谁也读不回来。
    存储库的对象名、`exact_version`、`sha256` 字段是**同一个字符串**，所以只有一个渲染点。
    """
    return "sha256:" + digest_hex


def check_view(lines, search, cursor, limit):
    """展示参数的形状：越界/类型不对是请求不成立（`invalid_request`），不是内部故障。"""
    if lines is not None:
        if (not isinstance(lines, (list, tuple)) or len(lines) != 2 or
                any(isinstance(v, bool) or not isinstance(v, int) or v < 1 for v in lines) or lines[0] > lines[1]):
            raise ArtifactReadRefused("invalid_request", "lines 须为 [start, end]（从 1 开始，start ≤ end）", en="lines must be [start, end] (from 1, start <= end)")
    if search is not None and (not isinstance(search, str) or not search or len(search) > 500):
        raise ArtifactReadRefused("invalid_request", "search 须为 1..500 字符的文本", en="search must be text of 1..500 characters")
    valid_cursor = (isinstance(cursor, int) and not isinstance(cursor, bool) and cursor >= 1 or
                    isinstance(cursor, str) and re.fullmatch(r"[1-9][0-9]*:[1-9][0-9]*", cursor))
    if cursor is not None and (not valid_cursor or search is not None and isinstance(cursor, str)):
        raise ArtifactReadRefused("invalid_request", "cursor 须为行号或返回的续读游标", en="cursor must be a line number or a returned continuation cursor")
    if limit is not None and (isinstance(limit, bool) or not isinstance(limit, int) or limit < 1):
        raise ArtifactReadRefused("invalid_request", "limit 必须是 ≥1 的整数", en="limit must be an integer >= 1")



def _text_or_none(data):
    if b"\x00" in data[:65536]:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _page(lines, first, last, offset=0):
    """Page whole lines when possible; continue an oversized line at a UTF-8 byte boundary."""
    out, used, end, continuation = [], 0, first - 1, None
    for i in range(first - 1, last):
        raw = lines[i].encode("utf-8")
        start = offset if i == first - 1 else 0
        if start:
            try:
                if start >= len(raw):
                    raise ValueError()
                raw[:start].decode("utf-8")
            except (ValueError, UnicodeDecodeError):
                raise ArtifactReadRefused("invalid_request", "无效的续读游标", en="Invalid continuation cursor")
        remaining = raw[start:]
        if out and used + len(remaining) + 1 > MAX_BYTES:
            break
        if len(remaining) + 1 > MAX_BYTES:
            fragment = remaining[:MAX_BYTES - 1].decode("utf-8", "ignore")
            consumed = len(fragment.encode("utf-8"))
            out.append(fragment); end = i + 1
            continuation = {"line": end, "offset": start + consumed, "kept_bytes": consumed, "line_bytes": len(raw) + 1}
            break
        out.append(remaining.decode("utf-8")); used += len(remaining) + 1; end = i + 1
    return out, end, continuation


def _cursor(cursor):
    return tuple(map(int, cursor.split(":"))) if isinstance(cursor, str) else (cursor or 1, 0)


def view(data, *, lines=None, search=None, cursor=None, limit=None, compare_data=None):
    """Render a bounded view of `data` (see module doc). Never claims a partial answer is the whole."""
    check_view(lines, search, cursor, limit)
    size = len(data)
    text = _text_or_none(data)
    if text is None:
        # ★ W20：这段文字原来说"在你的工作区用原生工具打开"——但提交的字节**不在**
        #   审核者的工作区（它只在成果库）。现在只说这里确实没有文本预览，
        #   并把"可原生打开"的那一份路径交给调用方（`image_view` / `stored_object_path`）补。
        return {"kind": "binary", "size": size,
                "note": "二进制成果：这里不提供文本预览（本页只读文本视图）；字节与 exact_version 一致",
                "note_en": "Binary result: no text preview here (this view only renders text); the bytes match exact_version"}
    all_lines = text.splitlines()
    total = len(all_lines)
    if compare_data is not None:
        other = _text_or_none(compare_data)
        if other is None:
            return {"kind": "diff", "comparable": False, "note": "其中一版不是文本，无法逐行比较",
                    "size": size}
        # ★ W209：diff 也按**行**分页，用的就是正文那一套字段与读法
        #   （`cursor` / `limit` / `has_more` / `start_line` / `end_line`）：以前这里一次答完，
        #   超过 MAX_BYTES 就只剩前一段，**没有继续查看的路**（W204 明确报出的合同差额）。
        # 普通页面按行号续读；超长行返回含 UTF-8 字节偏移的续读游标。
        # truncated 仍与 has_more 同义，老消费方不会把片段当完整。
        diff = list(difflib.unified_diff(other.splitlines(), all_lines, "before", "after", lineterm="", n=2))
        dtotal = len(diff)
        want = min(limit or DEFAULT_LINES, MAX_LINES)
        first, offset = _cursor(cursor)
        if (dtotal and first > dtotal) or (offset and not dtotal):
            raise ArtifactReadRefused("invalid_request",
                                      f"起始行 {first} 超出末尾（差异共 {dtotal} 行）",
                                      en=f"Start line {first} is past the end ({dtotal} diff lines)")
        last = min(dtotal, first + want - 1) if dtotal else 0
        out, end, cut = _page(diff, first, last, offset)
        more = bool(cut) or end < dtotal
        next_cursor = f"{cut['line']}:{cut['offset']}" if cut else end + 1 if more else None
        return {"kind": "diff", "comparable": True, "changed": bool(diff), "content": "\n".join(out),
                "start_line": first if dtotal else 0, "end_line": end,
                "diff_lines_total": dtotal,
                "total_lines": total,                 # 这一版文件的行数（**不是**差异行数，原意保留）
                "has_more": more, "cursor": next_cursor,
                # 这一段是不是完整：后面还有行，或者这一页里有一行只给了开头
                "truncated": bool(more or cut),
                "line_truncated": cut,                # 被切片的那一行（没有就是 null）
                "size": size}
    if search is not None:
        start = cursor or 1
        matches, used, next_cursor = [], 0, None
        for i in range(start - 1, total):
            if search in all_lines[i]:
                excerpt = all_lines[i][:500]
                cost = len(excerpt.encode("utf-8")) + 1
                if len(matches) >= MAX_MATCHES or used + cost > MAX_BYTES:
                    next_cursor = i + 1
                    break
                matches.append({"line": i + 1, "text": excerpt})
                used += cost
        return {"kind": "search", "query": search, "matches": matches, "total_lines": total,
                "has_more": next_cursor is not None, "cursor": next_cursor}
    want = min(limit or DEFAULT_LINES, MAX_LINES)
    first, offset = _cursor(cursor)
    if lines is not None:
        if cursor is None:
            first = lines[0]
        if first < lines[0] or first > lines[1]:
            raise ArtifactReadRefused("invalid_request", "cursor 超出行范围", en="cursor is outside the requested lines")
        last = min(lines[1], total)
    else:
        last = min(total, first + want - 1)
    if (total and first > total) or (offset and not total):
        raise ArtifactReadRefused("invalid_request", f"起始行 {first} 超出末尾（共 {total} 行）", en=f"Start line {first} is past the end ({total} lines)")
    out, end, cut = _page(all_lines, first, last, offset)
    more = bool(cut) or end < (last if lines is not None else total)
    next_cursor = f"{cut['line']}:{cut['offset']}" if cut else end + 1 if more else None
    return {"kind": "text", "total_lines": total, "start_line": first if total else 0, "end_line": end,
            "content": "\n".join(out), "has_more": more, "cursor": next_cursor, "size": size}
