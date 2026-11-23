import hashlib
import json
import re


SYSTEM_MAIL_SENDER = "postoffice"
_WORK_NOTICE_SYSTEMS = {
    "work.blocked", "work.assigned", "work.returned", "work.unblocked", "work.stage_accepted",
    "work.review_requested", "work.unassigned", "work.review_unassigned", "work.cancelled",
}

NEED_PURE_NEG = frozenset({"仅告知", "无需回复", "无需单独回复", "不用回复", "无需操作", "无需处理",
                           "回执默认不答复", "默认不答复", "fyi", "fyionly",
                           "noreply", "noaction", "forinformation", "noactionrequired",
                           "noreplybydefault", "receiptnoreplybydefault", "noreplyneeded",
                           "noactionneeded"})

_NEED_STRIP = re.compile(r"[\s，,。.;；:：!！?？（）()\[\]【】\"'“”‘’、/]+")


def letter_read_accepts(letter_id, direction, accept_on_read, surface):
    return bool(letter_id and direction == "inbox" and accept_on_read is True
                and surface in ("pi", "opencode", "codex", "claude"))


def normalize_need(text):
    """`需要：` 的值 → 用于整句相等的规范形（去空白/标点/大小写、去掉重复的 `需要：` 前缀）。"""
    v = re.sub(r"^(?:需要：)+", "", (text or "").strip()).strip()
    return _NEED_STRIP.sub("", v).lower()
def informational_need(need, *, system=False):
    """告知类只由整句明确的纯否定/信息标记表达，混合内容保守地视为请求。"""
    return (system and not (need or "").strip()) or normalize_need(need) in NEED_PURE_NEG


def letter_fields(row, state, fields=("subject", "need", "body")):
    raw = state["content_override_json"] if "content_override_json" in state.keys() else None
    try:
        override = json.loads(raw) if raw else {}
    except ValueError:
        override = {}
    if not isinstance(override, dict):
        override = {}
    return {key: override[key] if isinstance(override.get(key), str) else row[key] or "" for key in fields}


class MailOperations:
    def _letter_send_need(self, intent, need):
        if intent is None:
            return need
        if intent == "fyi" and (need is None or (isinstance(need, str) and informational_need(need))):
            return "FYI"
        if (intent == "request" and isinstance(need, str) and need.strip()
                and not informational_need(need)):
            return need
        raise self.CoreError("invalid_request",
                             "intent 选 fyi（省略 need，说明写在正文）或 request（need 填实际行动）",
                             en="Choose intent=fyi (omit need, put details in the body) or intent=request (state the action in need)",
                             field="intent")

    def _arm_batch_due(self, conn, recipients, *, touched, now_ts, need, informational=None):
        """Register or keep **one** `due(batch)` per logical unit, in this transaction (§1.3).

        The selection of units is `delivery.due.batch_units_for` — it lives there because the due
        table is that module's, and because it is then a judgement that can be exercised without a
        Core. What is decided here is only which of this letter's recipients count and whether the
        `need` asks for anything, using the module's one `informational_need`.
        """
        from delivery import due as due_mod
        # A wrong type used to surface as `float("2026-11-19 07:57:48")` deep inside `due`, naming
        # neither the caller nor the argument. This seam is the one place that knows the answer, so it
        # speaks instead of forwarding a failure it can name.
        if informational is None:
            informational = informational_need(need or "")
        if isinstance(now_ts, bool) or not isinstance(now_ts, (int, float)):
            raise self.CoreError(
                "invalid_request", "now_ts 必须是 epoch 秒（数值），收到 %s" % type(now_ts).__name__,
                en="now_ts must be epoch seconds (a number), got %s" % type(now_ts).__name__,
                reason_code="now_ts_not_epoch")
        return [row["due_key"] for row in due_mod.arm_batch_for_recipients(
            conn, recipients, touched=touched, informational=informational, now_ts=now_ts)]

    def _system_letter_with_fact(self, conn, fact_key, boxes):
        """The system letter that already carried this fact to this set of units, or None.

        Matching is on `meta_json.fact_key` **and** the recipient set: the same fact can legitimately
        be told to a different set of people (a responsibility moved), and that is a different
        sentence for a different reader, not a duplicate. `fact_key` alone would suppress the second
        one and leave the new responsible person uninformed.
        """
        wanted = set(boxes)
        rows = conn.execute(
            "SELECT letter_id FROM letters WHERE from_actor=? AND "
            "(CASE WHEN json_valid(meta_json) THEN json_extract(meta_json,'$.fact_key') END)=?",
            (self.SYSTEM_MAIL_SENDER, fact_key)).fetchall()
        for row in rows:
            if set(self._letter_boxes(conn, row["letter_id"])) == wanted:
                return row["letter_id"]
        return None

    @staticmethod
    def _letter_content(row, st):
        """收件内容的唯一选择器：这一箱看到的 subject/need/body 与 content_version。

        原文永远在 letters；只有该箱的 content_override_json 里实际改过的字段替换原文（抄送项从不带
        override，读原文）。读信、短索引、投递条目、导出投影都走这里，不各自拼正文。"""
        out = letter_fields(row, st)
        keys = st.keys() if hasattr(st, "keys") else ()
        raw = st["content_override_json"] if "content_override_json" in keys else None
        out["content_version"] = (st["content_version"] if "content_version" in keys else None) or 1
        out["body_sha256"] = (hashlib.sha256(out["body"].encode("utf-8")).hexdigest()
                              if raw else row["body_sha256"])
        return out
    def system_letter(self, *, letter_id, boxes, subject, body, need="", kind, headers=None, now_ts=None,
                      fact_key=None, work_ref=None, sub_ref=None, conn=None, _observed_ctx=None):
        """邮局自己的系统信（闹钟、广播汇总、交接、对外发送被拒）：由持有 Core 的进程内宿主（邮差）记入同一信件账，
        发信人固定为 postoffice。不是线路操作（不在 handle 里），会话无法以它的名义发信。
        幂等 = letter_id + 正文；不是普通信（meta 标 system），不能被改撤。

        `conn=` writes on a connection the caller already has, without opening or committing a
        transaction: a due worker must put the letter in the **same** transaction as the fire that
        decided to send it, or a failure between them leaves a stall recorded as told. Callers that
        pass no `conn` keep the old shape exactly (own transaction, own commit).
        """
        if not isinstance(letter_id, str) or not letter_id.strip():
            raise self.CoreError("invalid_request", "letter_id 必须是非空字符串", en="letter_id must be a non-empty string")
        if not isinstance(body, str) or not body or not isinstance(subject, str):
            raise self.CoreError("invalid_request", "subject/body 必须是字符串，body 非空", en="subject/body must be strings and body non-empty")
        boxes = list(dict.fromkeys(b for b in boxes if isinstance(b, str) and b))
        if not boxes:
            raise self.CoreError("invalid_request", "boxes 必须是非空信箱列表", en="boxes must be a non-empty list of boxes")
        self._check_export_headers(headers)
        # ★ W223：系统信也要把**每一箱的导出信头**记下来（没给就记空列表）。
        #   原来 `headers is None` ⇒ meta 里没有 `export_headers` ⇒ `letter_exports(box)` 永不列这封信
        #   ⇒ 邮差 tick 的 `export_missing_letters()`（UC07 的既有补导出）**看不见**它：桥写的
        #   idle-alert 只在 Core 里"state=delivered"，收件箱永远没有文件，也不会被补出来。
        #   普通信走 `write_letter()` 时 `heads` 恒为 `{box: list(extra_headers)}`（可能是空列表），
        #   所以只有"系统信不传 headers"这一条路掉队。这里是那唯一接缝。
        heads = {b: (list(headers) if headers is not None else []) for b in boxes}
        meta = {"system": kind, "export_headers": heads}
        if work_ref is not None:
            meta["work_ref"] = work_ref
        if sub_ref is not None:
            meta["sub_ref"] = sub_ref
        if fact_key is not None:
            # §1.3 "`meta_json.fact_key` 按事实及责任去重": the fact, plus who must act on it, is
            # what makes two sends the same send. Stored beside `system` so a reader can answer
            # "was this fact already told to this unit" from the letter itself.
            if not isinstance(fact_key, str) or not fact_key.strip():
                raise self.CoreError("invalid_request", "fact_key 必须是非空字符串",
                                     en="fact_key must be a non-empty string")
            meta["fact_key"] = fact_key
        calc = hashlib.sha256(body.encode("utf-8")).hexdigest()
        # `nullcontext(conn)` is not a shortcut: with a caller-provided connection this method must
        # neither begin nor commit, because the transaction is the caller's (the due worker's), and
        # the only useful atomicity is the one that includes the decision to send.
        from contextlib import nullcontext
        txn = nullcontext(conn) if conn is not None else self._txn()
        with txn as conn:
            if _observed_ctx is not None:
                # Trusted postman only: validate the observed owner's binding atomically
                # with this system letter; no model-facing system-letter operation.
                self._verify_trusted_ctx(conn, self._parse_trusted_ctx(_observed_ctx))
            now_num = time.time() if now_ts is None else float(now_ts)
            now = self._now(now_num)
            if fact_key is not None:
                # Deduplicated inside the write transaction: two senders reporting the same fact
                # cannot both pass this check, and the loser returns the winner's id instead of
                # writing a second letter with a second deadline.
                existing = self._system_letter_with_fact(conn, fact_key, boxes)
                if existing is not None:
                    return {"letter_id": existing, "body_sha256": calc, "boxes": boxes,
                            "deduplicated": True}
            row = conn.execute("SELECT body_sha256 FROM letters WHERE letter_id=?", (letter_id,)).fetchone()
            if row is not None and row["body_sha256"] != calc:
                raise self.CoreError("conflict", "同名信件已存在且正文不同", en="A letter with this name already exists with a different body", reason_code="letter_id_taken",
                                     letter_id=letter_id)
            if row is None:
                conn.execute("INSERT INTO letters(letter_id, from_actor, created_at, subject, body, body_sha256,"
                             " need, meta_json) VALUES(?,?,?,?,?,?,?,?)",
                             (letter_id, self.SYSTEM_MAIL_SENDER, now, subject, body, calc, need or "",
                              self.canonical_json(meta)))
            have = set(self._letter_boxes(conn, letter_id))
            added = {}
            for b in boxes:
                if b not in have:
                    conn.execute("INSERT INTO letter_states(letter_id, box, state, updated_at, category)"
                                 " VALUES(?,?,?,?,?)", (letter_id, b, "delivered", now, "to"))
                    added[b] = heads[b]
            if added:
                self._merge_export_headers(conn, letter_id, added)
            self._arm_batch_due(conn, [{"box": b} for b in added], touched=list(added), now_ts=now_num, need=need,
                                informational=informational_need(need, system=True))
        return {"letter_id": letter_id, "body_sha256": calc, "boxes": boxes}
    def _letter_boxes(self, conn, letter_id):
        return [r["box"] for r in conn.execute(
            "SELECT box FROM letter_states WHERE letter_id=? ORDER BY box", (letter_id,)).fetchall()]

    @classmethod
    def _check_export_headers(cls, headers):
        """The extra header lines of one recipient's inbox file (UC07: Core keeps them so the postman can
        re-export the very same bytes if the sender's export never landed). Single-line `键：值` only; the
        envelope lines (来源/事由/需要) are always written from the letter itself, never from here."""
        if headers is None:
            return
        if not isinstance(headers, list) or len(headers) > 20:
            raise cls.CoreError("invalid_request", "recipients[].headers 必须是不超过 20 行的字符串数组", en="recipients[].headers must be an array of at most 20 strings")
        for h in headers:
            if not isinstance(h, str) or not h or "\n" in h or "\r" in h or len(h) > 500 or "：" not in h:
                raise cls.CoreError("invalid_request", "recipients[].headers 每行必须是单行「键：值」（≤500 字）", en="Each recipients[].headers line must be a single line 'key：value' (<=500 characters)")
            if h.split("：", 1)[0].strip() in cls._ENVELOPE_KEYS:
                raise cls.CoreError("invalid_request", "recipients[].headers 不能写来源/事由/需要（这三行由信本身给出）", en="recipients[].headers cannot set source/subject/need (the letter itself gives those three lines)")

    @staticmethod
    def _imported_headers(raw):
        """`meta_json` 里记录过的导出信头 —— **唯一**的读法（坏/陌生形状一律当没有）。

        `export_headers` 是写信那一刻记下的「这一箱该有的那几行信头」（`键：值`）。读它的地方
        原本有六处，各自决定"JSON 坏了算什么、不是对象算什么、不是数组算什么"：五处 `except
        (ValueError, AttributeError)`、一处只接 `ValueError`（坏 JSON 在那一处被当成"有派生 meta"），
        还有一处直接 `.get` 在解析结果上。于是同一份坏数据在不同读口得到不同答案，而
        `meta_json` 是**非空 JSON 数组**时，列表读口会抛 `AttributeError` 把整次读取打断。

        这里只回答一个问题：这份 meta 里那份按箱分组的状态是什么。返回 dict（没有/坏 ⇒ `{}`），
        由调用方决定"没有这一箱的信头"对它意味着什么（导出跳过 / 列表给 None / 判派生信）。
        """
        if not raw:
            return {}
        try:
            meta = json.loads(raw)
        except (TypeError, ValueError):
            return {}
        if not isinstance(meta, dict):
            return {}
        heads = meta.get("export_headers")
        return heads if isinstance(heads, dict) else {}

    @staticmethod
    def _imported_header_lines(raw, box):
        """`_imported_headers` 的单箱视图：这一箱的行（不是数组 ⇒ None，不猜成空集）。"""
        heads = MailOperations._imported_headers(raw).get(box)
        return heads if isinstance(heads, list) else None

    @staticmethod
    def _derived_meta(raw):
        """True when the letter carries meta beyond its recorded export headers (a notice, system mail...)."""
        if not raw:
            return False
        try:
            meta = json.loads(raw)
        except ValueError:
            return True
        return not isinstance(meta, dict) or bool(set(meta) - {"export_headers"})

    @staticmethod
    def _merge_export_headers(conn, letter_id, heads):
        if not heads:
            return
        row = conn.execute("SELECT meta_json FROM letters WHERE letter_id=?", (letter_id,)).fetchone()
        try:
            meta = json.loads(row["meta_json"] or "{}") if row else {}
        except ValueError:
            meta = {}
        meta.setdefault("export_headers", {}).update(heads)
        conn.execute("UPDATE letters SET meta_json=? WHERE letter_id=?",
                     (json.dumps(meta, ensure_ascii=False), letter_id))

    def _letter_not_found(self, letter_id):
        """"There is no such letter" — the one frame for it.

        Three entries answer this question (read one, accept one, archive one) and each used to
        spell the frame out in full. They agreed, which is the dangerous kind: the frame an employee
        sees would start depending on which entry they used, as soon as one copy gained a
        `reason_code` or its wording was tightened. The reads themselves stay separate — accept
        needs existence only, the other two need the row, and folding them together would make
        accept load a letter body it never uses. One frame, three queries.

        The revise/retract path deliberately keeps a **wider** frame ("no such letter, or it is not
        in that inbox") because there the ambiguity is real; it is not merged here.
        """
        return self.CoreError("not_found", f"信件 {letter_id!r} 不存在",
                              en=f"Letter {letter_id!r} does not exist", letter_id=letter_id)

    #: 但**不是待办**：告知/回执/抄送不再算成谁的未办事项（取消「归档作为义务」的落点）。
    NO_ACTION_CLASSES = ("notice", "receipt", "cc")

    @staticmethod
    def _now_text(row):
        """`accepted_at` 的读侧形状：与 Core 其它时间列一致的文本时间；没有就是 None（不编）。

        列里存的就是 `_now()` 的文本（与 `updated_at`/`created_at` 同一形状）。万一遇到以
        epoch 秒写下的行（本特性第一版曾用 REAL），也照实转成同一形状 —— 读侧只出一种形状。
        """
        try:
            v = row["accepted_at"]
        except (KeyError, IndexError, TypeError):
            return None
        if v is None or v == "":
            return None
        if isinstance(v, str):
            return v
        try:
            return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(float(v)))
        except (TypeError, ValueError, OSError):
            return None

    @classmethod
    def _letter_lifecycle(cls, row, st, facts=None, box=None):
        """Derive this recipient's class and reply status from current content and receipts."""
        # The recipient this judgment is FOR. A single-letter read passes the `letter_states` row
        # (which carries `box`); a list read passes a joined row that does not, so the caller names
        # it. Without it the direction-sensitive notices below cannot tell whose box this is.
        if not isinstance(box, str) or not box:
            box = st["box"] if "box" in st.keys() else None
        reason = (st["state_reason"] if "state_reason" in st.keys() else None) or None
        archived = (st["state"] == "archived")
        # 通道受理事实（`letter_accept` 写的两列）。旧库/旧行没有它 ⇒ None = **未知**，
        # 不是"没受理"也不是"已受理"；`cc` 不需要它（见下）。
        accepted = (st["accepted_at"] is not None) if "accepted_at" in st.keys() else None
        category = ((st["category"] if "category" in st.keys() else None) or "to")
        try:
            meta = json.loads(row["meta_json"] or "{}")
        except (TypeError, ValueError):
            meta = {}
        if not isinstance(meta, dict):
            meta = {}
        heads = meta.get("export_headers") or {}
        own_heads = []
        if isinstance(heads, dict):
            for lines in heads.values():
                own_heads.extend([x for x in (lines or []) if isinstance(x, str)])
        klass = "request"
        if category == "cc":
            klass = "cc"
        elif (meta.get("notice") and row["from_actor"] == cls.NOTICE_SENDER
              or meta.get("system") in _WORK_NOTICE_SYSTEMS and row["from_actor"] == cls.SYSTEM_MAIL_SENDER):
            klass = "work_notice"
        elif any(h.startswith("回执：") for h in own_heads):
            klass = "receipt"
        elif (informational_need(letter_fields(row, st, ("need",))["need"],
                                 system=row["from_actor"] == cls.SYSTEM_MAIL_SENDER)
              or meta.get("system") == "position.welcome" and row["from_actor"] == cls.SYSTEM_MAIL_SENDER):
            klass = "notice"
        def verdict(terminal, terminal_reason=None, *, needs_reply=False):
            return {"letter_class": klass, "terminal": terminal,
                    "terminal_reason": terminal_reason,
                    "needs_reply": needs_reply, "accepted": accepted}

        if klass == "cc":
            # 抄送副本：写入即终结。它不是待投递项（`round_begin` 跳过 cc），不存在"投递前被抽走"；
            # 收件人在 Core/页面上照旧看得到这份副本。
            return verdict(True, "accepted_notice")
        if klass in cls.NO_ACTION_CLASSES:
            # notice / receipt：**通道受理之后**才算不需要动作（没送到人眼前就收走 = 丢信）。
            return verdict(bool(accepted), "accepted_notice" if accepted else None)
        if klass == "work_notice":
            return cls._work_notice_lifecycle(row, classified_notice=meta, box=box,
                                               archived=archived, reason=reason,
                                               accepted=accepted, facts=facts)
        # request：回复过 / 被归档过就终结；否则仍是待办（"没有回应就算未处理"）。
        if reason == "replied":
            return verdict(True, "replied")
        if archived:
            return verdict(True, "retracted" if reason == "retracted" else "archived")
        return verdict(False, None, needs_reply=True)

    def _refuse_unit_not_carried(self, unit):
        """The refusal that goes with `_may_accept_for`: this session does not carry `unit`.

        ★ W436: this frame was written twice, character for character — here in `letter.archive` and in
          `lib/round.round_begin`. The judgement behind it was already one function (`_may_accept_for`,
          which both modules call), so the two copies could only ever drift apart in wording while
          claiming the same `unit_not_carried`. A refusal that says a different thing depending on which
          entry point refused it makes callers switch on prose.

        Not shared with the *other* two `unit_not_carried` frames (`round_owner_register`,
        `_record_round_letter_accept`): those say "no longer carries" / "the accepting unit", which are
        different answers about a different moment. Same code, different sentence — deliberately.
        """
        return self.CoreError("unauthorized_attempt", "当前受控会话未承载请求的岗位单元",
                              en="The current trusted session does not carry the requested position unit",
                              reason_code="unit_not_carried", unit=unit)

    def _holder_boxes_for_letter(self, conn, letter_id, actor):
        """这封信投进的所有箱里，**本会话此刻能替它们记受理**的那些（只认 Core 绑定事实）。

        返回**列表**（升序）。为什么是列表：一封信可以同时投进个人箱与若干岗位，"本会话能替
        哪几个记"天然可能多于一个 —— 那种情况下**选取权属于调用方**（`unit`），Core 不替他挑。
        B 在 `e5c9ddfd` 上的反例正是这个缺陷：旧实现 `ORDER BY ls.box` 取第一个，于是 dev 同时
        承载 `app.dev`/`app.qa` 时两次受理都落到 `app.dev`。
        """
        rows = conn.execute(
            "SELECT ls.box AS box FROM letter_states ls WHERE ls.letter_id=? ORDER BY ls.box",
            (letter_id,)).fetchall()
        out = []
        for r in rows:
            b = r["box"]
            if self._may_accept_for(conn, b, actor):
                out.append(b)
        return out

    def _letter_recipient_state(self, conn, actor, letter_id, unit):
        """Select the recipient row this session may update."""
        if conn.execute("SELECT 1 FROM letters WHERE letter_id=?", (letter_id,)).fetchone() is None:
            raise self._letter_not_found(letter_id)
        box = actor if unit is None else unit
        sql = "SELECT * FROM letter_states WHERE letter_id=? AND box=?"
        st = conn.execute(sql, (letter_id, box)).fetchone()
        if unit is None and st is None:
            boxes = self._holder_boxes_for_letter(conn, letter_id, actor)
            if len(boxes) > 1:
                raise self.CoreError("invalid_request",
                                     "这封信同时投进多个由本会话承载的箱（%s）；请用 unit 指明本次实际投递单元"
                                     % "、".join(boxes),
                                     en="This letter landed in several boxes carried by this session (%s); "
                                        "pass unit to name the delivery unit" % ", ".join(boxes),
                                     letter_id=letter_id, field="unit")
            if boxes:
                box = boxes[0]
                st = conn.execute(sql, (letter_id, box)).fetchone()
        if unit is not None and st is None:
            raise self.CoreError("forbidden", "这封信没有投进 %r 这一箱" % (unit,),
                                 en=f"The letter was not delivered to {unit!r}", letter_id=letter_id, box=actor)
        if st is None or (unit is not None and not self._may_accept_for(conn, box, actor)):
            raise self.CoreError("forbidden", "不是该信件的收件人",
                                 en="Not a recipient of this letter", letter_id=letter_id, box=actor)
        return box, st

    @classmethod
    def _terminal_report(cls, conn, rows, *, box):
        """★ W406：`letters_terminal` 的**判定本体**抽成类方法 —— 建轮/留箱检查这些
        "只有只读连接、没有 Core 实例"的调用方（`_round_core_facts`）读同一份判据，
        不再各自实现一遍"哪些算结清"。

        输入 `rows` = 上面的 select 形状（l.* + letter_states 派生列）；返回
        `(terminal_ids, reason_map, class_map, state_map, needs_reply_map)`，其中后两张
        覆盖**全部**行（不只是终结的）—— 提醒侧需要"还没终结、但本来就不用动手"的那一档。
        """
        _facts = cls._work_notice_facts(conn, [cls._notice_work_ref(r["meta_json"]) for r in rows])
        done, why, kind = [], {}, {}
        state, needs_reply = {}, {}
        for r in rows:
            life = cls._letter_lifecycle(r, r, facts=_facts, box=box)
            kind[r["letter_id"]] = life["letter_class"]
            state[r["letter_id"]] = r["state"]
            needs_reply[r["letter_id"]] = bool(life["needs_reply"])
            if life["terminal"]:
                done.append(r["letter_id"])
                why[r["letter_id"]] = life["terminal_reason"]
        return done, why, kind, state, needs_reply

    BODY_PAGE_CHARS, BODY_PAGE_MAX = 8000, 20000

    @classmethod
    def _body_page(cls, body, cursor, limit):
        """单封读口的正文页：短信（一页放得下、从头读）原样不动、不带分页字段；长信默认只给一页，
        `body_page.cursor` 是续读位置（正文第几个字符，从 1 起）。同一个 `cursor`/`limit` 形状
        （≥1 的整数）与 `has_more`/`cursor` 语义沿用 `r/artifact_read` 的正文视图；单位是字符而不是行，
        因为一行就能超限的长信按行翻不动，而各页首尾相接必须逐字等于已存正文。事实仍是同一份正文：
        `body_sha256`/`content_version` 不变，页只是读法，不存副本。"""
        for name, v in (("cursor", cursor), ("limit", limit)):
            if v is not None and (isinstance(v, bool) or not isinstance(v, int) or v < 1):
                raise cls.CoreError("invalid_request", "%s 必须是 ≥1 的整数" % name,
                                    en="%s must be an integer >= 1" % name, field=name)
        start, want, n = cursor or 1, min(limit or cls.BODY_PAGE_CHARS, cls.BODY_PAGE_MAX), len(body)
        if start == 1 and n <= want:
            return {"body": body}
        if start > n:
            raise cls.CoreError("invalid_request", "起始位置 %d 超出正文末尾（共 %d 字符）" % (start, n),
                                en="Start position %d is past the end of the body (%d characters)" % (start, n), field="cursor")
        end = min(n, start - 1 + want)
        return {"body": body[start - 1:end], "body_page": {
            "start": start, "end": end, "total_chars": n, "has_more": end < n, "cursor": end + 1 if end < n else None}}

    def _letter_recipient_unit(self, conn, actor, letter_id, default_box, allowed=None):
        """Select the carried primary copy first, then cc; prefer the actor within each category."""
        rows = conn.execute("SELECT box FROM letter_states WHERE letter_id=?"
                            " AND COALESCE(category,'') IN ('','to','cc')"
                            " ORDER BY COALESCE(category='cc',0), box!=?, box",
                            (letter_id, default_box))
        return next((box for (box,) in rows if (allowed is None or box in allowed)
                     and self._may_accept_for(conn, box, actor)), default_box)

    def letter_read(self, actor, *, letter_id=None, target=None, direction="inbox", include_archived=False,
                    cursor=None, limit=None, accept_on_read=False, now_ts=None):
        """A native tool opening an inbound letter can record delivery in the same transaction."""
        self._check_actor(actor)
        if direction not in ("inbox", "sent"):
            raise self.CoreError("invalid_request", "direction 必须是 inbox 或 sent",
                                 en="direction must be inbox or sent")
        box = actor if target is None else target
        if not isinstance(box, str) or not box.strip():
            raise self.CoreError("invalid_request", "target 必须是非空信箱名", en="target must be a non-empty mailbox name")
        box = box.strip().removeprefix("@")
        receipt = letter_read_accepts(letter_id, direction, accept_on_read, (self._pending_ctx_ctx() or {}).get("surface"))
        with self._txn(read_only=not receipt) as conn:
            if letter_id and target is None and direction != "sent":
                box = self._letter_recipient_unit(conn, actor, letter_id, box)
            is_position_box = self._is_position_address(conn, box)
            if (direction != "sent" or not letter_id) and box != actor:
                can_read_position = is_position_box and (
                    self._on_duty(conn, actor, box) or self._may_accept_for(conn, box, actor))
                if (not can_read_position and actor != self._request_user()
                        and not (letter_id and self._mail_read_approved(conn, actor, box, letter_id))):
                    raise self.CoreError("forbidden", "读取他人信箱需要组织审批",
                                         en="Reading another person's mailbox requires organization approval",
                                         reason_code="organization_request_required", target=box)
            if direction == "sent":
                derived_headers = ("回执：", "广播：", "闹钟：", "切换事件：", "升级求助：", "抄送副本：")

                def has_derived_header(row, recipient):
                    headers = self._imported_header_lines(row["meta_json"], recipient) or []
                    return any(isinstance(header, str) and header.startswith(derived_headers)
                               for header in headers)

                if letter_id:
                    recipient = box
                    row = conn.execute(
                        "SELECT l.*, s.box AS recipient_box, s.state, s.logical_address, s.category, "
                        "s.content_override_json, s.content_version, s.retracted_at "
                        "FROM letters l JOIN letter_states s ON s.letter_id=l.letter_id "
                        "WHERE l.from_actor=? AND l.letter_id=? AND s.box=? "
                        "AND (s.category IS NULL OR s.category='to') "
                        "AND s.state<>'archived' AND s.retracted_at IS NULL",
                        (actor, letter_id, recipient)).fetchone()
                    if (row is None or row["from_actor"] == self.NOTICE_SENDER
                            or self._derived_meta(row["meta_json"])
                            or has_derived_header(row, recipient)):
                        raise self.CoreError("not_found", "没有这封本人发出的普通主送信",
                                             en="No such ordinary main-recipient letter sent by this actor",
                                             letter_id=letter_id)
                    c = self._letter_content(row, row)
                    heads = self._imported_header_lines(row["meta_json"], recipient)
                    return {"ok": True, "box": recipient, "state": row["state"], "letter": {
                        "letter_id": row["letter_id"], "from": row["from_actor"],
                        "created_at": row["created_at"], "subject": c["subject"], "need": c["need"],
                        **self._body_page(c["body"], cursor, limit), "body_sha256": c["body_sha256"],
                        "content_version": c["content_version"], "category": row["category"] or "to",
                        "logical_address": row["logical_address"],
                        "headers": heads,
                        "system": False}}
                rows = conn.execute(
                    "SELECT l.*, s.box AS recipient_box, s.state, s.logical_address, s.category, "
                    "s.content_override_json, s.content_version, s.retracted_at "
                    "FROM letters l JOIN letter_states s ON s.letter_id=l.letter_id "
                    "WHERE l.from_actor=? AND (s.category IS NULL OR s.category='to') "
                    "AND s.state<>'archived' AND s.retracted_at IS NULL "
                    "ORDER BY l.created_at DESC, l.letter_id, s.box", (box,)).fetchall()
                out = []
                for r in rows:
                    if (r["from_actor"] == self.NOTICE_SENDER or self._derived_meta(r["meta_json"])
                            or has_derived_header(r, r["recipient_box"])):
                        continue
                    c = self._letter_content(r, r)
                    heads = self._imported_header_lines(r["meta_json"], r["recipient_box"])
                    out.append({"letter_id": r["letter_id"], "to": r["recipient_box"],
                                "from": r["from_actor"], "created_at": r["created_at"],
                                "subject": c["subject"], "need": c["need"], "body": c["body"],
                                "body_sha256": c["body_sha256"], "content_version": c["content_version"],
                                "state": r["state"], "category": r["category"] or "to",
                                "logical_address": r["logical_address"],
                                "headers": heads})
                return {"ok": True, "box": box, "direction": "sent", "count": len(out), "letters": out}
            if letter_id:
                row = conn.execute("SELECT * FROM letters WHERE letter_id=?", (letter_id,)).fetchone()
                if row is None:
                    raise self._letter_not_found(letter_id)
                st = conn.execute("SELECT * FROM letter_states WHERE letter_id=? AND box=?",
                                  (letter_id, box)).fetchone()
                if st is None:
                    raise self.CoreError("forbidden", "不是该信件的收件人", en="Not a recipient of this letter", letter_id=letter_id, box=box)
                if st["state"] == "archived" and not include_archived:
                    raise self.CoreError("not_found", "该信件在本箱已归档（默认读路径不返回）", en="This letter is archived in this box (the default read path does not return it)", letter_id=letter_id)
                c = self._letter_content(row, st)
                page = self._body_page(c["body"], cursor, limit)
                if (receipt and st["state"] == "delivered" and st["accepted_at"] is None
                        and st["category"] != "cc" and self._may_accept_for(conn, box, actor)):
                    accepted = self._record_round_letter_accept(conn, actor, box, letter_id, "direct_read", now_ts)
                    st = {**st, "accepted_at": accepted["accepted_at"], "accepted_channel": accepted["channel"]}
                heads = self._imported_header_lines(row["meta_json"], box)
                _life = self._letter_lifecycle(
                    row, st,
                    facts=self._work_notice_facts(conn, [self._notice_work_ref(row["meta_json"])]))
                return {"ok": True, "box": box, "state": st["state"], "letter": {
                    "letter_id": row["letter_id"], "from": row["from_actor"],
                    "created_at": row["created_at"], "subject": c["subject"], "need": c["need"],
                    **page, "body_sha256": c["body_sha256"],
                    "content_version": c["content_version"], "category": st["category"] or "to",
                    "logical_address": st["logical_address"],
                    "headers": heads,
                    # ★ W362：这封信对收件人还要不要动作（派生值；事实仍是 state/state_reason）
                    **_life,
                    "accepted_at": self._now_text(st),
                    "accepted_channel": (st["accepted_channel"]
                                         if "accepted_channel" in st.keys() else None),
                    "system": row["from_actor"] == self.NOTICE_SENDER or self._derived_meta(row["meta_json"]),
                    "system_kind": self._system_kind(row["meta_json"])}}
            paged = limit is not None or cursor is not None
            where = " FROM letter_states s JOIN letters l USING(letter_id) WHERE s.box=?"
            if not include_archived:
                where += " AND s.state<>'archived'"
            params = [box]
            if paged:
                if limit is not None and (type(limit) is not int or not 1 <= limit <= 200):
                    raise self.CoreError("invalid_request", "limit 须为 1..200 的整数",
                                         en="limit must be an integer 1..200", field="limit")
                limit = limit or 50
                count = conn.execute("SELECT count(*)" + where, params).fetchone()[0]
                if cursor is not None:
                    anchor = (conn.execute("SELECT l.created_at, l.letter_id FROM letter_states s"
                                           " JOIN letters l USING(letter_id) WHERE s.box=? AND l.letter_id=?",
                                           (box, cursor)).fetchone() if isinstance(cursor, str) else None)
                    if anchor is None:
                        raise self.CoreError("invalid_request", "请使用本箱列表返回的 next_cursor",
                                             en="Use this inbox's next_cursor", field="cursor")
                    where += " AND (l.created_at,l.letter_id)<(?,?)"
                    params.extend(anchor)
            order = " ORDER BY l.created_at, l.letter_id"
            if paged:
                order = " ORDER BY l.created_at DESC, l.letter_id DESC LIMIT ?"
                params.append(limit + 1)
            rows = conn.execute(
                "SELECT l.letter_id, l.from_actor, l.created_at, l.subject, l.need, l.meta_json, l.body_sha256,"
                " CASE WHEN s.content_override_json IS NOT NULL THEN l.body END AS body,"
                " s.state, s.logical_address, s.category, s.content_override_json, s.content_version,"
                " s.state_reason, s.accepted_at, s.accepted_channel" + where + order, params).fetchall()
            more = paged and len(rows) > limit
            if more:
                rows.pop()
            _facts = self._work_notice_facts(conn, [self._notice_work_ref(r["meta_json"]) for r in rows])
            out = []
            for r in rows:
                if r["content_override_json"]:
                    c = self._letter_content(r, r)
                else:
                    c = {**letter_fields(r, r, ("subject", "need")), "body_sha256": r["body_sha256"],
                         "content_version": r["content_version"] or 1}
                heads = self._imported_header_lines(r["meta_json"], box)
                out.append({"letter_id": r["letter_id"], "from": r["from_actor"], "created_at": r["created_at"],
                            "subject": c["subject"], "body_sha256": c["body_sha256"],
                            "content_version": c["content_version"], "state": r["state"],
                            "category": r["category"] or "to", "need": c["need"],
                            "headers": heads,
                            # ★ W362：待办/计数只认这个派生值，不再让每个消费者自己拿 kind+need 推。
                            **self._letter_lifecycle(r, r, facts=_facts, box=box),
                            # 受理事实的原始字段（通道 + 时间）：`terminal` 的判据就在它上面，
                            # 读侧要核"凭什么说送到了"时不必再问一次。
                            "accepted_at": self._now_text(r),
                            "accepted_channel": (r["accepted_channel"]
                                                 if "accepted_channel" in r.keys() else None),
                            "system": r["from_actor"] == self.NOTICE_SENDER or self._derived_meta(r["meta_json"]),
                            "system_kind": self._system_kind(r["meta_json"])})
            result = {"ok": True, "box": box, "count": count if paged else len(rows), "letters": out}
            if paged:
                result.update(has_more=more, next_cursor=rows[-1]["letter_id"] if more else None)
            return result

    @staticmethod
    def _system_kind(raw):
        try:
            meta = json.loads(raw or "{}")
        except (TypeError, ValueError):
            return None
        value = meta.get("system") if isinstance(meta, dict) else None
        return value if isinstance(value, str) else None

    def _record_letter_reply(self, conn, actor, letter_id, reply_to, recipients, keep_open, now):
        """Link the sent reply and file only the recipient unit that this session can answer for."""
        if not isinstance(reply_to, str) or reply_to == letter_id:
            return {"original": "not_filed"}
        originals = conn.execute(
            "SELECT s.box FROM letter_states s JOIN letters l ON l.letter_id=s.letter_id"
            " WHERE s.letter_id=? AND COALESCE(s.category,'to')='to' AND s.retracted_at IS NULL"
            " AND l.from_actor NOT IN (?,?)",
            (reply_to, self.NOTICE_SENDER, self.SYSTEM_MAIL_SENDER)).fetchall()
        boxes = [r["box"] for r in originals if self._may_accept_for(conn, r["box"], actor)]
        if not boxes:
            return {"original": "not_filed"}
        if actor not in boxes and len(boxes) > 1:
            raise self.CoreError("invalid_request", "请从要回复的主送岗位发信",
                                 en="Reply using the intended primary recipient position", letter_id=reply_to)
        box = self._letter_recipient_unit(conn, actor, reply_to, actor, boxes)
        self._merge_export_headers(conn, letter_id, {
            r["box"]: ["回复原信：" + reply_to] for r in recipients if r.get("category") != "cc"
        })
        conn.execute("UPDATE letter_states SET state=CASE WHEN ? THEN state ELSE 'archived' END,"
                     " state_reason='replied', updated_at=? WHERE letter_id=? AND box=?",
                     (bool(keep_open), now, reply_to, box))
        return {"reply_to": reply_to, "original": "kept_open" if keep_open else "filed"}

    @staticmethod
    def _notice_work_ref(raw):
        """`meta_json` 里的 `work_ref`（读不懂/没有 ⇒ None）。只用于批量取事实，不参与判定。"""
        if not raw:
            return None
        try:
            meta = json.loads(raw)
        except (TypeError, ValueError):
            return None
        ref = meta.get("work_ref") if isinstance(meta, dict) else None
        return ref if isinstance(ref, str) and ref else None
    @classmethod
    def _work_notice_facts(cls, conn, work_refs):
        """Batch the current Work, waiting segment and effective submission facts used in mail."""
        refs = [r for r in {str(x) for x in (work_refs or ()) if x}]
        out = {}
        if not refs:
            return out
        for i in range(0, len(refs), 500):                   # SQLite 变量上限内分批
            chunk = refs[i:i + 500]
            # `waiting_json` 是 `wait_resolved` 那一支需要的（同一条查询、同一张表，不新增查询）。
            q = ("SELECT work_ref, state, assignee_ref, reviewer_ref, waiting_json,"
                 " CASE WHEN json_valid(waiting_json) THEN json_extract(waiting_json,'$.key') END AS wait_key"
                 " FROM work WHERE work_ref IN (%s)" % ",".join("?" * len(chunk)))
            for r in conn.execute(q, chunk).fetchall():
                out[r["work_ref"]] = {"state": r["state"], "assignee": r["assignee_ref"],
                                      "reviewer": r["reviewer_ref"], "sub": None,
                                      "waiting": bool(r["waiting_json"]), "wait_key": r["wait_key"]}
        if not out:
            return out
        # 当前待审提交：同一批 Work 的最新 effective 提交（与 `_current_sub` 同表同义，只批量取）。
        found = list(out)
        for i in range(0, len(found), 500):
            chunk = found[i:i + 500]
            q = ("SELECT work_ref, sub_ref, submitted_by FROM subs WHERE state=? AND work_ref IN (%s)"
                 % ",".join("?" * len(chunk)))
            for r in conn.execute(q, [cls.SUB_EFFECTIVE] + chunk).fetchall():
                out[r["work_ref"]]["sub"] = r["sub_ref"]
                reviewer = out[r["work_ref"]]["reviewer"]
                holder = conn.execute("SELECT holder FROM position_holders WHERE position=?", (reviewer,)).fetchone()
                person = holder[0] if holder else reviewer
                if person:
                    from office_core import _work_module
                    same = _work_module("work_runnable.py", "office_work_runnable").same_submitter(
                        conn, r["work_ref"], r["sub_ref"], r["submitted_by"], reviewer, person)
                    out[r["work_ref"]]["review_unavailable"] = same is not False
        return out
    @classmethod
    def _work_notice_lifecycle(cls, row, *, classified_notice, box=None, archived, reason,
                               accepted, facts=None):
        """Decide whether a Work notice still needs delivery or action."""
        def verdict(terminal, terminal_reason=None):
            return {"letter_class": "work_notice", "terminal": terminal,
                    "terminal_reason": terminal_reason, "needs_reply": False,
                    "accepted": accepted}

        click = classified_notice if isinstance(classified_notice, dict) else {}
        system = click.get("system") or ""
        notice = click.get("notice")
        if not notice and system in _WORK_NOTICE_SYSTEMS:
            notice = "assigned" if system == "work.unblocked" else system.removeprefix("work.")
        if (archived and reason == "obsolete") or click.get("actionable") is False:
            return verdict(True, "work_obsolete")
        f = (facts or {}).get(click.get("work_ref")) or {}
        if notice in ("cancelled", "unassigned", "review_unassigned"):
            if (notice == "unassigned" and f.get("assignee") == box
                    or notice == "review_unassigned" and f.get("state") == "Submitted" and f.get("reviewer") == box):
                return verdict(True, "work_obsolete")
            if archived:
                return verdict(True, "archived")
            return verdict(bool(accepted), "accepted_notice" if accepted else None)
        if f:
            state, assignee = f.get("state"), f.get("assignee")
            if state in (cls.WORK_DONE, cls.WORK_CANCELLED):
                return verdict(True, "work_obsolete")
            if system == "work.blocked" and f["wait_key"] != str(click.get("fact_key") or "").removesuffix(":escalated"):
                return verdict(True, "work_obsolete")
            if notice in ("assigned", "returned", "invalidated", "stage_accepted"):
                if state != cls.WORK_ACTIVE or isinstance(assignee, str) and box and assignee != box:
                    return verdict(True, "work_obsolete")
            if notice == "wait_resolved" and (state != cls.WORK_ACTIVE or not f.get("waiting")):
                return verdict(True, "work_obsolete")
            if notice == "review_requested":
                want = click.get("sub_ref")
                if state != "Submitted" or f.get("reviewer") != box or f.get("review_unavailable") or want and want != f.get("sub"):
                    return verdict(True, "work_obsolete")
        return verdict(False)
    def letter_accept(self, actor, *, letter_id, channel, act_key, unit=None, now_ts=None):
        """Record the first delivery receipt for an exact current recipient, with replay."""
        self._check_actor(actor)
        if not isinstance(channel, str) or not channel.strip() or "\n" in channel:
            raise self.CoreError("invalid_request", "channel 必须是非空单行字符串",
                                 en="channel must be a non-empty single-line string")
        channel = channel.strip()[:64]
        if unit is not None and (not isinstance(unit, str) or not unit):
            raise self.CoreError("invalid_request",
                                 "unit 显式给了就必须是非空字符串",
                                 en="unit, when given, must be a non-empty string", field="unit")
        # ★ 幂等参数包含 unit：同一 act_key 不能用来给**不同单元**表态（两个业务动作）。
        params = {"letter_id": letter_id, "channel": channel, "unit": unit}
        with self._txn() as conn:
            replay, digest = self.storage.action_replay(conn, actor, "letter.accept", act_key, params)
            if replay is not None:
                return replay
            box, st = self._letter_recipient_state(conn, actor, letter_id, unit)
            result = self._write_letter_accept(conn, actor, box, letter_id, st, channel, now_ts)
            self.storage.record_action(conn, actor, "letter.accept", act_key, digest, result, now_ts)
            return result

    def _write_letter_accept(self, conn, actor, box, letter_id, state, channel, now_ts):
        now = self._now(now_ts)
        already = state["accepted_at"] is not None
        if not already:
            conn.execute("UPDATE letter_states SET accepted_at=?, accepted_channel=? "
                         "WHERE letter_id=? AND box=?", (now, channel, letter_id, box))
        result = {"ok": True, "letter_id": letter_id, "box": box,
                  "accepted_at": state["accepted_at"] if already else now,
                  "channel": state["accepted_channel"] if already else channel,
                  "already_accepted": already, "acted_at": now}
        if box != actor:
            result["accepted_for_holder"] = box
        return result

    def _carried_position_units(self, conn, binding_id):
        rows = conn.execute(
            "SELECT target.binding_id FROM bindings target"
            " JOIN bindings me ON me.binding_id = ?"
            " WHERE target.actor = target.binding_id"
            " AND target.state = 'active' AND target.revoked_at IS NULL"
            " AND me.state = 'active' AND me.revoked_at IS NULL"
            " AND me.host_session_ref <> ''"
            " AND target.host_session_ref = me.host_session_ref"
            " ORDER BY target.binding_id", (binding_id,)).fetchall()
        return [row[0] for row in rows if self._position_exists(conn, row[0])]

    def _may_accept_for(self, conn, box, actor):
        binding_id = (self._pending_ctx_ctx() or {}).get("binding_id")
        return box == actor or box in self._carried_position_units(conn, binding_id)

    def letters_terminal(self, box, *, unit=None, limit=500, only=None, now_ts=None):
        """Pure lifecycle projection for one mailbox unit.

        Direct host callers may continue to query their selected `box`. On the controlled
        `letter.terminal` endpoint, `box` is supplied from the trusted actor and optional `unit`
        selects a carried position unit. When `unit` differs from the trusted actor, current carry
        is checked inside the same read transaction as the requested-ref query. `only` is a narrow
        projection: every requested ref is either projected for this unit or reported missing.
        """
        self._check_actor(box)
        target_box = box if unit is None else unit
        if not isinstance(target_box, str) or not target_box:
            raise self.CoreError("invalid_request", "unit 必须是非空字符串", en="unit must be a non-empty string", field="unit")
        select = ("SELECT l.*, s.state, s.logical_address, s.category, s.content_override_json,"
                  " s.content_version, s.state_reason, s.accepted_at, s.accepted_channel"
                  " FROM letters l"
                  " JOIN letter_states s ON s.letter_id=l.letter_id WHERE s.box=?")
        wanted = None
        if only is not None:
            wanted = [str(x) for x in only if x]
            limit = max(int(limit), len(wanted))
        with self._txn(read_only=True) as conn:
            storage_box = target_box
            if target_box != box:
                if not self._may_accept_for(conn, target_box, box):
                    raise self._refuse_unit_not_carried(target_box)
            if wanted == []:
                return {"ok": True, "box": target_box, "status": "complete", "requested": [],
                        "missing": [], "terminal": [], "terminal_by_ref": {}, "reason": {},
                        "class": {}, "state": {}, "needs_reply": {}, "truncated": False}
            if wanted is None:
                rows = conn.execute(select + " ORDER BY l.created_at", (storage_box,)).fetchall()
            else:
                rows = []
                for i in range(0, len(wanted), 500):
                    chunk = wanted[i:i + 500]
                    rows.extend(conn.execute(
                        select + " AND l.letter_id IN (%s) ORDER BY l.created_at"
                        % ",".join("?" * len(chunk)), [storage_box] + chunk).fetchall())
            done, why, cls, state, needs_reply = self._terminal_report(conn, rows, box=storage_box)
            truncated = len(done) > limit
            terminal_set = set(done)
            missing = [lid for lid in wanted if lid not in state] if wanted is not None else []
            done = done[:limit]
            return {"ok": True, "box": target_box,
                    "status": "partial" if (missing or truncated) else "complete",
                    "requested": wanted, "missing": missing, "terminal": done,
                    "terminal_by_ref": {k: k in terminal_set for k in state},
                    "reason": {k: why[k] for k in done}, "class": cls,
                    "state": state, "needs_reply": needs_reply, "truncated": truncated}
    def _letter_mutation_target(self, conn, actor, letter_id, box, content_version):
        """改撤的同一组业务判据（发信人本人 · 普通直发 · 主送项 · 未被读/归档/撤回 · 版本相符）。"""
        row = conn.execute("SELECT * FROM letters WHERE letter_id=?", (letter_id,)).fetchone()
        st = conn.execute("SELECT * FROM letter_states WHERE letter_id=? AND box=?",
                          (letter_id, box)).fetchone() if row is not None else None
        if row is None or st is None:
            raise self.CoreError("not_found", "没有这封信（或它不在该收件箱）", en="No such letter (or it is not in that inbox)", letter_id=letter_id, box=box)
        if row["from_actor"] != actor:
            raise self.CoreError("forbidden", "只能改撤自己发出的信", en="You can only revise or retract letters you sent", reason_code="not_sender",
                                 letter_id=letter_id)
        if row["from_actor"] == self.NOTICE_SENDER or self._derived_meta(row["meta_json"]):
            raise self.CoreError("conflict", "系统提醒/派生信不能改撤", en="System notices and derived letters cannot be revised or retracted", reason_code="not_ordinary_letter",
                                 letter_id=letter_id)
        if (st["category"] or "to") != "to":
            raise self.CoreError("conflict", "抄送副本保留发出时的内容，不能单独改撤", en="A cc copy keeps the content it was sent with and cannot be revised or retracted on its own", reason_code="cc_copy",
                                 letter_id=letter_id, box=box)
        if st["retracted_at"] is not None:
            raise self.CoreError("conflict", "这封信已撤回", en="This letter has already been retracted", reason_code="retracted", letter_id=letter_id)
        if st["state"] != "delivered":
            # ★ W353：措辞只说**能证到的事实**。`delivered` = 已入账（见 schema 注释），所以只有
            #   "已经不是入账态"这一条；"已读"不是 Core 写过的状态（`read` 无写入口），不能拿它
            #   当理由——收件方是否真处理过在 `acks.jsonl`/归档里，由读侧如实呈现。
            raise self.CoreError("conflict", "收件方已归档（或这封信已不在待投状态：%s），不能再改撤；要更正请另发一封更正信" % st["state"],
                                 en="The recipient has archived it (or it is no longer in a deliverable state: %s), so it can no longer be revised or retracted. Send a correction letter instead" % st["state"],
                                 reason_code="already_received", letter_id=letter_id, state=st["state"])
        if st["accepted_at"] is not None or conn.execute(
                "SELECT 1 FROM round_ref WHERE box=? AND ref=? AND ref_state='active'", (box, letter_id)).fetchone():
            raise self.CoreError("conflict", "此信正在投递或已受理，更正请另发一封信",
                                 en="Delivery is in progress or accepted. Send a correction as a separate letter.",
                                 reason_code="delivery_started", letter_id=letter_id, box=box)
        cur = st["content_version"] or 1
        if content_version is not None and content_version != cur:
            raise self.CoreError("conflict", "内容版本已变，请重新查看", en="The content version changed; look again", reason_code="stale_version",
                                 letter_id=letter_id, current_version=cur)
        return row, st
    def letter_revise(self, actor, *, letter_id, box, act_key, subject=None, need=None, body=None,
                      content_version=None, now_ts=None):
        """C07 改信：只换这一箱主送项的 subject/need/body（原文与抄送不变），content_version+1。"""
        self._check_actor(actor)
        fields = {"subject": subject, "need": need, "body": body}
        given = {k: v for k, v in fields.items() if v is not None}
        if not given:
            raise self.CoreError("invalid_request", "至少要改 subject / need / body 之一", en="Change at least one of subject / need / body")
        for k, v in given.items():
            if not isinstance(v, str) or (k != "body" and "\n" in v):
                raise self.CoreError("invalid_request", f"{k} 必须是{'' if k == 'body' else '单行'}字符串", en=f"{k} must be a {'' if k == 'body' else 'single-line '}string")
        if "body" in given and not given["body"]:
            raise self.CoreError("invalid_request", "body 不能为空", en="body cannot be empty")
        params = {"letter_id": letter_id, "box": box, "content_version": content_version,
                  "fields": {k: hashlib.sha256(v.encode("utf-8")).hexdigest() for k, v in given.items()}}
        with self._txn() as conn:
            replay, digest = self.storage.action_replay(conn, actor, "letter.revise", act_key, params)
            if replay is not None:
                return replay
            row, st = self._letter_mutation_target(conn, actor, letter_id, box, content_version)
            try:
                ov = json.loads(st["content_override_json"] or "{}")
            except ValueError:
                ov = {}
            ov.update(given)
            now = self._now(now_ts)
            ver = (st["content_version"] or 1) + 1
            conn.execute("UPDATE letter_states SET content_override_json=?, content_version=?, updated_at=?"
                         " WHERE letter_id=? AND box=?", (self.canonical_json(ov), ver, now, letter_id, box))
            st2 = conn.execute("SELECT * FROM letter_states WHERE letter_id=? AND box=?",
                               (letter_id, box)).fetchone()
            c = self._letter_content(row, st2)
            from delivery import due
            at = due.now() if now_ts is None else now_ts
            unit = st["logical_address"] or box
            if not informational_need(c["need"]):
                self._arm_batch_due(conn, [{"box": box, "logical_address": unit}], touched=[box], now_ts=at, need=c["need"])
            else:
                due.push_back(conn, "batch", unit, at)
            result = {"ok": True, "letter_id": letter_id, "box": box, "changed": sorted(given),
                      "content_version": ver, "subject": c["subject"], "need": c["need"], "body": c["body"],
                      "cc_boxes": [r["box"] for r in conn.execute(
                          "SELECT box FROM letter_states WHERE letter_id=? AND category='cc' ORDER BY box",
                          (letter_id,))], "acted_at": now}
            self.storage.record_action(conn, actor, "letter.revise", act_key, digest, result, now_ts)
            return result
    def letter_retract(self, actor, *, letter_id, box, act_key, content_version=None, now_ts=None):
        """C07 撤回：只把这一箱的主送项标为撤回并退出待投（state=archived, reason=retracted）；不删原文。"""
        self._check_actor(actor)
        params = {"letter_id": letter_id, "box": box, "content_version": content_version}
        with self._txn() as conn:
            replay, digest = self.storage.action_replay(conn, actor, "letter.retract", act_key, params)
            if replay is not None:
                return replay
            self._letter_mutation_target(conn, actor, letter_id, box, content_version)
            now = self._now(now_ts)
            conn.execute("UPDATE letter_states SET state='archived', state_reason='retracted', retracted_at=?,"
                         " updated_at=? WHERE letter_id=? AND box=?", (now, now, letter_id, box))
            result = {"ok": True, "letter_id": letter_id, "box": box, "retracted_at": now,
                      "cc_boxes": [r["box"] for r in conn.execute(
                          "SELECT box FROM letter_states WHERE letter_id=? AND category='cc' ORDER BY box",
                          (letter_id,))], "acted_at": now}
            self.storage.record_action(conn, actor, "letter.retract", act_key, digest, result, now_ts)
            return result
    def letter_archive(self, actor, *, letter_id, act_key, unit=None, now_ts=None):
        """归档 = 该箱的状态迁移（不删行、不删正文）；重放幂等。"""
        self._check_actor(actor)
        params = {"letter_id": letter_id}
        if unit is not None:
            if not isinstance(unit, str) or not (unit := unit.strip().removeprefix("@")):
                raise self.CoreError("invalid_request", "unit 要填收件信箱", en="unit must name a recipient mailbox", field="unit")
            params["unit"] = unit
        with self._txn() as conn:
            replay, digest = self.storage.action_replay(conn, actor, "letter.archive", act_key, params)
            if replay is not None:
                return replay
            box, st = self._letter_recipient_state(conn, actor, letter_id, unit)
            now = self._now(now_ts)
            already = (st["state"] == "archived")
            if not already:
                conn.execute("UPDATE letter_states SET state='archived', updated_at=?"
                             " WHERE letter_id=? AND box=?", (now, letter_id, box))
            result = {"ok": True, "letter_id": letter_id, "box": box, "state": "archived",
                      "already_archived": already, "acted_at": now}
            self.storage.record_action(conn, actor, "letter.archive", act_key, digest, result, now_ts)
            return result
