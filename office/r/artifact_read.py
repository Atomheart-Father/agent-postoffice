"""R package: controlled artifact read. Core decides access; this module renders the verified stream.

One source: a submitted artifact -- the exact bytes snapshotted at publish time under their digest (never the
mutable working copy). Loading those bytes and verifying their digest live in ONE place,
`lib/work_result.read_version` (what `work.read` runs); this module only renders them.

Views (all bounded; a partial answer says so and how to continue):
  lines=[start,end] (1-based, inclusive) · search="text"
  cursor=<line number or returned long-line continuation>. Text is UTF-8; anything else is reported as binary with its size -- never decoded
  as if it were text, never claimed to be previewable."""
import re
from codecs import getincrementaldecoder
from itertools import chain, groupby
from operator import itemgetter

DEFAULT_LINES = 200
MAX_LINES = 2000
MAX_BYTES = 128 * 1024                 # per answer, not per object
MAX_MATCHES = 100


class ArtifactReadRefused(Exception):
    """Refusal with both wordings; the host picks one by the reader's session language (`text`)."""
    def __init__(self, code, message, en=None):
        super().__init__(message); self.code = code; self.message = message; self.message_en = en

    def text(self, locale="zh"):
        return self.message_en if locale == "en" and self.message_en else self.message


def version_name(digest_hex):
    """发布时把内容摘要编码为成果版本令牌。"""
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



def _cursor(cursor):
    return tuple(map(int, cursor.split(":"))) if isinstance(cursor, str) else (cursor or 1, 0)


_LINE_ENDS = "\n\r\v\f\x1c\x1d\x1e\x85\u2028\u2029"


def _fragments(chunks, needs_lines, search=None):
    """Split as str.splitlines(), carrying only decoder state and a possible CR."""
    decoder = getincrementaldecoder("utf-8")()
    line, prefix, cr = 1, 65536, ""
    for block in chain(chunks, (b"",)):
        if prefix and b"\0" in block[:prefix]:
            raise UnicodeError("binary prefix")
        prefix = max(0, prefix - len(block))
        text = cr + decoder.decode(block, final=not block)
        cr = "\r" if block and text.endswith("\r") else ""
        if cr:
            text = text[:-1]
        pieces = text.splitlines(keepends=True)
        if pieces and not needs_lines(line, line + len(pieces) - 1):
            # When the whole block is outside the page, count it without visiting every line.
            unfinished = pieces[-1][-1] not in _LINE_ENDS
            yield line + len(pieces) - 1, ""
            line += len(pieces) - unfinished
            continue
        if search is not None and len(pieces) > 2 and search not in text:
            first = pieces[0]
            yield line, first[:-2] if first.endswith("\r\n") else first[:-1]
            line += len(pieces) - 1
            yield line - 1, ""
            pieces = pieces[-1:]
        for piece in pieces:
            if piece[-1] in _LINE_ENDS:
                yield line, piece[:-2] if piece.endswith("\r\n") else piece[:-1]
                line += 1
            else:
                yield line, piece


def _search_stream(chunks, search, start):
    matches, used, next_cursor, total = [], 0, None, 0
    overlap = len(search) - 1
    rows = groupby(_fragments(chunks, lambda _, end: next_cursor is None and end >= start, search), key=itemgetter(0))
    for total, pieces in rows:
        if total < start or next_cursor is not None:
            continue
        excerpt, tail, matched = "", "", False
        for _, piece in pieces:
            excerpt += piece[:500 - len(excerpt)]
            if not matched:
                window = tail + piece
                matched = search in window
                tail = window[-overlap:] if overlap else ""
        if matched:
            cost = len(excerpt.encode("utf-8")) + 1
            if len(matches) >= MAX_MATCHES or used + cost > MAX_BYTES:
                next_cursor = total
            else:
                matches.append({"line": total, "text": excerpt})
                used += cost
    return {"kind": "search", "query": search, "matches": matches, "total_lines": total,
            "has_more": next_cursor is not None, "cursor": next_cursor}


def _stream_line(pieces, offset):
    kept = bytearray()
    seen = 0
    for _, piece in pieces:
        raw = piece.encode("utf-8")
        start = max(0, offset - seen)
        room = MAX_BYTES - len(kept)
        if start < len(raw) and room:
            kept.extend(raw[start:start + room])
        seen += len(raw)
    if offset and (offset >= seen or kept[0] & 0xC0 == 0x80):
        raise ArtifactReadRefused("invalid_request", "无效的续读游标", en="Invalid continuation cursor")
    return kept, seen - offset


def _page_stream(chunks, lines, cursor, limit):
    first, offset = _cursor(cursor)
    if lines is not None:
        first = lines[0] if cursor is None else first
        if not lines[0] <= first <= lines[1]:
            raise ArtifactReadRefused("invalid_request", "cursor 超出行范围", en="cursor is outside the requested lines")
        last = lines[1]
    else:
        last = first + min(limit or DEFAULT_LINES, MAX_LINES) - 1
    out, used, total, end, cut, full = [], 0, 0, first - 1, None, False
    rows = groupby(_fragments(chunks, lambda a, b: not full and a <= last and b >= first), key=itemgetter(0))
    for total, pieces in rows:
        if total < first or total > last or full:
            continue
        start = offset if total == first else 0
        kept, size = _stream_line(pieces, start)
        if out and used + size + 1 > MAX_BYTES:
            full = True
            continue
        end = total
        if size + 1 > MAX_BYTES:
            text = kept[:MAX_BYTES - 1].decode("utf-8", "ignore")
            cut = f"{end}:{start + len(text.encode('utf-8'))}"
            out.append(text)
            full = True
        else:
            out.append(kept.decode("utf-8"))
            used += size + 1
    if (total and first > total) or (offset and not total):
        raise ArtifactReadRefused("invalid_request", f"起始行 {first} 超出末尾（共 {total} 行）",
                                  en=f"Start line {first} is past the end ({total} lines)")
    more = cut is not None or end < (min(last, total) if lines is not None else total)
    return {"kind": "text", "total_lines": total, "start_line": first if total else 0, "end_line": end,
            "content": "\n".join(out), "has_more": more, "cursor": cut or (end + 1 if more else None)}


def _binary():
    return {"kind": "binary",
            "note": "二进制成果：这里不提供文本预览（本页只读文本视图）；字节与 exact_version 一致",
            "note_en": "Binary result: no text preview here (this view only renders text); the bytes match exact_version"}


def stream_view(chunks, *, lines=None, search=None, cursor=None, limit=None):
    check_view(lines, search, cursor, limit)
    try:
        return (_search_stream(chunks, search, cursor or 1) if search is not None
                else _page_stream(chunks, lines, cursor, limit))
    except UnicodeError:
        return _binary()
