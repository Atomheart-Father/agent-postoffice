"""R package: controlled publish seam. Turns the files an employee selected in its scope into the exact
change_set Core's work.submit records. Digest and ref are computed HERE, never taken from the wire.

The base every `files[].path` is read under is **this Work's working directory**: what `workspace-map[scope]`
maps to on this machine (R2-F). It is not the mailbox directory and it does not follow the shell's cwd; when
the scope has no mapping the caller refuses instead of falling back.

Every read is fd-relative from that directory with O_NOFOLLOW; a file must be a single-link regular file
inside it (never `_priv`). Bytes are streamed: the digest is computed over exactly the bytes copied into the
host-owned store, so the digest stays verifiable after the employee changes its working copy. Any byte format is
accepted (text, binary, media). A deletion is a tombstone: the path must be absent now; no empty file is invented.

All-or-nothing: every selected file is snapshotted into private temp objects first; only when all succeeded are
they renamed under their digest names and the change_set returned. A failure leaves no object under a final name
and Core is never asked to reference bytes that are not stored. Authority stays with Core."""
import hashlib, os, secrets, stat
import artifact_read as ART   # 只借 `version_name`：`exact_version` 那个令牌的拼法只有一份（见其 docstring）

#: Configurable safety cap (not a product file limit). OFFICE_PUBLISH_MAX_BYTES overrides; 0 = no cap.
MAX_BYTES = int(os.environ.get("OFFICE_PUBLISH_MAX_BYTES") or 4 * 1024 ** 3)
CHUNK = 1024 * 1024
#: 一条交付项只有两种：写一份新字节，或删掉一份（墓碑）。
#:
#: ★ W436 v19：`add` / `modify` 退出。它们是为**中间候选**留的词表 —— 当年想区分"新增/修改/写入"，
#:   但 `work.submit` 的 change_set 从来只记 `write` / `delete`（产品库 17,563 项：write 17,048、
#:   delete 515，`add`/`modify` 各 0），工具说明也早就是"op 只在删除时写 delete"，CLI 与适配器
#:   都不产生这两个值。留着它们的唯一后果是：同一个文件"是写还是删"有四种说法，而实际只有两种。
OPS = ("write", "delete")


class PublishRefused(Exception):
    """Refusal with both wordings; the host picks one by the publisher's session language (`text`)."""
    def __init__(self, message, en=None):
        super().__init__(message); self.message = message; self.message_en = en

    def text(self, locale="zh"):
        return self.message_en if locale == "en" and self.message_en else self.message


def carries_prefix(rel, scope, *, exact=False):
    """这个路径是不是**带着 `scope` 前缀**写出来的 —— 一处判据，两种用法。

    两种用法问的是同一句话（"这段路径写没写交付范围前缀"），只是要的东西不同：

      · `_Where.strip_prefix`（`exact=False`）：在**已经选定**的那个范围上把前缀剥掉，
        所以只认 `<scope>/…`；光写 `<scope>` 不是文件，原样留着让"文件不存在"去回答。
      · `endpoint._scope_prefix_of`（`exact=True`）：在**若干候选**里挑这个路径属于哪个范围，
        `<scope>` 本身也算命中 —— 它指的就是那个工作空间的根。

    以前两处各写一遍条件（`path == s or path.startswith(s + "/")` 与 `rel.startswith(prefix + "/")`），
    逐例对拍 14 组只有 `rel == scope` 一组不同，也就是下头这个开关。把它写成参数而不是留两份
    手写条件，是因为这条差别必须**看得见**：`exact=True` 命中的那种路径到了 publish 侧剥不掉
    前缀，会以"文件不存在"拒绝（它本来就是个目录），这是既有行为，不是这里要改的东西。
    """
    if not isinstance(rel, str) or not isinstance(scope, str) or not scope:
        return False
    if exact and rel == scope:
        return True
    return rel.startswith(scope + "/")


class _Where:
    """The four facts every path refusal has to carry, built from the same values the read used:

    * the path exactly as received;
    * where it resolves to (the working directory + that path);
    * the one allowed root, and this Work's delivery root (the ref prefix);
    * what to write instead.

    One object per publish call, so the wording can never drift from the read: `root` is the directory the
    files are opened under — **this Work's working directory**, i.e. what `workspace-map[scope]` maps to on
    this machine (R2-F; the host resolves it, never the wire, and it is neither the mailbox directory nor
    whatever the shell's cwd happens to be), and `prefix` is the Work's scope. There is no second base and no
    second root to explain: a path that is not under `root` is refused with these same facts.
    """

    def __init__(self, scope, prefix):
        self.root = os.path.abspath(scope)
        self.prefix = prefix or ""

    # ---- facts -----------------------------------------------------------
    def resolved(self, rel):
        return os.path.join(self.root, rel) if isinstance(rel, str) and not os.path.isabs(rel) else rel

    def strip_prefix(self, rel):
        """`repo/app/export.py` — the Work's scope written again — is the same file as `export.py`.

        R2-C refused it as a doubled layer; R2-F normalises it instead, because with the base now being the
        mapped working directory a model may legitimately spell the path from the workspace and both
        spellings have to land on one file (one ref, never `repo/app/repo/app/...`)."""
        if carries_prefix(rel, self.prefix):
            return rel[len(self.prefix) + 1:]
        return rel

    def _tail(self):
        zh = [f"允许的根：{self.root}（files[].path 相对这个工作目录）"]
        en = [f"allowed root: {self.root} (files[].path is relative to this working directory)"]
        if self.prefix:
            zh.append(f"本 Work 的交付根：{self.prefix}（提交里每个文件的 ref 是 {self.prefix}/<path>）")
            en.append(f"this Work's delivery root: {self.prefix} (every submission ref is {self.prefix}/<path>)")
        return zh, en

    @staticmethod
    def _join(head, tail, hint, sep):
        out = [head] + tail
        if hint:
            out.append(hint)
        return sep.join(out)

    # ---- refusals --------------------------------------------------------
    def outside(self, rel):
        """A path that leaves the working directory (absolute, `..`, or the host's private area)."""
        inside = None
        if isinstance(rel, str) and os.path.isabs(rel):
            try:                                            # an absolute path that is simply spelled wrong
                _r = os.path.relpath(rel, self.root)
            except ValueError:
                _r = None
            if _r and not _r.startswith("..") and not os.path.isabs(_r):
                inside = self.strip_prefix(_r.replace(os.sep, "/"))
        if inside:
            hint = (f"这个位置就在工作目录里，去掉前缀写成相对路径 `{inside}`",
                    f"that location is inside the working directory: drop the prefix and write the relative path `{inside}`")
        else:
            hint = ("只能写相对工作目录的路径，例如 `export.py`（不要以 / 开头、不要用 ..）",
                    "write a path relative to the working directory, e.g. `export.py` (no leading /, no ..)")
        return PublishRefused(self._join(f"路径不在工作目录内：{rel!r}", self._tail()[0], hint[0], "；"),
                              en=self._join(f"path is outside the working directory: {rel!r}", self._tail()[1], hint[1], "; "))

    def not_found(self, rel):
        """The named file is not under the working directory — say where it was looked for and what to write."""
        zh, en = self._tail()
        zh.insert(0, f"解析到：{self.resolved(rel)}")
        en.insert(0, f"resolved to: {self.resolved(rel)}")
        h = self.hint(rel)
        return PublishRefused(self._join(f"文件不存在：{rel}", zh, h and h[0], "\n"),
                              en=self._join(f"file not found: {rel}", en, h and h[1], "\n"))

    def admit(self, rel):
        """Normalise the path (the Work's scope written again is the same file) and check that it stays inside
        the working directory. Returns the path to read — the ref uses the same one."""
        rel = self.strip_prefix(rel)
        _check_rel(rel, self)
        return rel

    def hint(self, rel):
        """A concrete correction for a path that is not there, when one can be named without guessing."""
        if not isinstance(rel, str) or not rel:
            return None
        p = self.prefix
        base = rel.rsplit("/", 1)[-1]
        root_has = os.path.join(self.root, base)
        if base != rel and os.path.lexists(root_has):           # same file already at the working-directory root
            return (f"建议：工作目录下已有 `{base}`，写 `{base}` 就是 {p + '/' if p else ''}{base}",
                    f"suggestion: the working directory already holds `{base}`; write `{base}` to submit it as "
                    f"{p + '/' if p else ''}{base}")
        return None


def _check_rel(rel, where):
    """路径判据只有一处：非空相对路径、不含 NUL、没有空段或 `.`/`..` 段（不许越出工作目录），
    第一段不能是 `_priv`（宿主自己的私有区，不该当交付内容写进成果库）。

    拒绝时**只**走 `where.outside()` —— 它带着"允许的根 / 本 Work 的交付根 / 该怎么写"三句话；
    "拒绝长什么样"只有这一处说了算。
    """
    if not isinstance(rel, str) or not rel or rel.startswith("/") or "\x00" in rel:
        raise where.outside(rel)
    parts = rel.split("/")
    if any(p in ("", ".", "..") for p in parts) or parts[0] == "_priv":
        raise where.outside(rel)
    return parts


def _open_parent(scope_fd, parts, fds):
    cur = scope_fd
    for d in parts[:-1]:
        try:
            cur = os.open(d, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=cur)
        except FileNotFoundError:
            return None
        except OSError as e:
            raise PublishRefused(f"目录不可用：{d!r}（{e.strerror}）", en=f"directory not usable: {d!r} ({e.strerror})")
        fds.append(cur)
    return cur


def _absent(scope_fd, rel, where):
    parts = _check_rel(rel, where)
    fds = []
    try:
        cur = _open_parent(scope_fd, parts, fds)
        if cur is None:
            return True
        try:
            os.stat(parts[-1], dir_fd=cur, follow_symlinks=False)
            return False
        except FileNotFoundError:
            return True
    finally:
        for f in fds:
            os.close(f)


def _snapshot(scope_fd, rel, cap, store_fd, where):
    """Stream one file into a private temp in the store (or only hash it when there is no store).
    Returns (digest_hex, size, temp_name|None). Refuses symlinks, hard links, non-regular files and a source that
    changed while it was being read."""
    parts = _check_rel(rel, where)
    fds = []
    tmp = None
    try:
        cur = _open_parent(scope_fd, parts, fds)
        if cur is None:
            raise where.not_found(rel)
        try:
            fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=cur)
        except FileNotFoundError:                      # the parent is there, the file is not: same four facts
            raise where.not_found(rel)
        except OSError as e:
            raise PublishRefused(f"文件不可用：{rel!r}（{e.strerror}）", en=f"file not usable: {rel!r} ({e.strerror})")
        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1:
                raise PublishRefused(f"不是单链接的普通文件：{rel!r}", en=f"not a single-link regular file: {rel!r}")
            if cap and st.st_size > cap:
                raise PublishRefused(f"文件超过配置的发布上限（{cap} 字节）：{rel!r}", en=f"file larger than the configured publish cap ({cap} bytes): {rel!r}")
            if store_fd is not None:
                try:
                    vfs = os.fstatvfs(store_fd)
                    if vfs.f_bavail * vfs.f_frsize < st.st_size:
                        raise PublishRefused(f"成果库空间不足，放不下 {rel!r}", en=f"not enough space in the artifact store for {rel!r}")
                except AttributeError:
                    pass
                tmp = f".tmp-{secrets.token_hex(8)}"
                out = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=store_fd)
            else:
                out = None
            h, size = hashlib.sha256(), 0
            try:
                while True:
                    chunk = os.read(fd, CHUNK)
                    if not chunk:
                        break
                    size += len(chunk)
                    if cap and size > cap:
                        raise PublishRefused(f"读取期间文件长到超过发布上限：{rel!r}", en=f"file grew beyond the publish cap while reading: {rel!r}")
                    h.update(chunk)
                    if out is not None:
                        view = memoryview(chunk)
                        while view:
                            view = view[os.write(out, view):]
                if out is not None:
                    os.fsync(out)
            finally:
                if out is not None:
                    os.close(out)
            after = os.fstat(fd)
            if size != st.st_size or after.st_size != st.st_size or after.st_mtime_ns != st.st_mtime_ns:
                raise PublishRefused(f"读取期间源文件变了：{rel!r}；请重新发布", en=f"source changed while it was being read: {rel!r}; publish again")
            return h.hexdigest(), size, tmp
        finally:
            os.close(fd)
    except BaseException:
        if tmp is not None:
            try: os.unlink(tmp, dir_fd=store_fd)
            except OSError: pass
        raise
    finally:
        for f in fds:
            os.close(f)


def _intact(store_fd, name, size):
    try:
        st = os.stat(name, dir_fd=store_fd, follow_symlinks=False)
    except OSError:
        return False
    if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1 or st.st_size != size:
        return False
    h = hashlib.sha256()
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=store_fd)
    try:
        while True:
            chunk = os.read(fd, CHUNK)
            if not chunk:
                break
            h.update(chunk)
    finally:
        os.close(fd)
    return h.hexdigest() == name


#: macOS shadow files on exFAT/external disks are not results. Only real AppleDouble content is skipped (counted).
APPLEDOUBLE_MAGIC = b"\x00\x05\x16\x07"


def is_appledouble(path, data=None):
    name = str(path).rsplit("/", 1)[-1]
    if name == ".__priv" or "/.__priv/" in "/" + str(path) + "/":
        return True
    if not name.startswith("._") or data is None:
        return False
    return bytes(data[:4]) == APPLEDOUBLE_MAGIC


def _head(scope_fd, rel, where):
    parts = _check_rel(rel, where)
    fds = []
    try:
        cur = _open_parent(scope_fd, parts, fds)
        if cur is None:
            return None                     # never fall back to the process cwd: dir_fd=None would read a stranger's file
        fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=cur)
        try:
            return os.read(fd, 4)
        finally:
            os.close(fd)
    except OSError:
        return None
    finally:
        for f in fds:
            os.close(f)


def _open_store(artifact_root):
    """成果库（宿主私有目录）的 fd；没有库 ⇒ None（只算摘要，不存对象）。"""
    if artifact_root is None:
        return None
    os.makedirs(artifact_root, mode=0o700, exist_ok=True)
    return os.open(artifact_root, os.O_RDONLY | os.O_DIRECTORY)


def _drop_temps(store_fd, temps):
    """删掉自己这一轮写的临时对象（**只删自己的**）。"""
    for tmp, _digest, _size in temps or ():
        if store_fd is None or tmp is None:
            continue
        try:
            os.unlink(tmp, dir_fd=store_fd)
        except OSError:
            pass


def _commit_temps(store_fd, temps):
    """把临时对象改名成摘要名（**固化**）。摘要名已存在且字节一致 ⇒ 只删临时对象。"""
    for tmp, digest, size in temps:
        if _intact(store_fd, digest, size):
            os.unlink(tmp, dir_fd=store_fd)
        else:
            os.rename(tmp, digest, src_dir_fd=store_fd, dst_dir_fd=store_fd)


def _plan_group(scope, prefix, files):
    """校验这一组的文件（**不碰成果库**）：返回 `(where, items)`。

    items 是 [(相对路径, op)]。路径规范与拒绝口径全在 `_Where`（一处）。
    """
    if not isinstance(files, list) or not files:
        raise PublishRefused("files 必须是非空列表", en="files must be a non-empty list")
    where = _Where(scope, prefix)
    items, seen = [], set()
    for it in files:
        if isinstance(it, str):
            it = {"path": it}
        if not isinstance(it, dict) or set(it) - {"path", "op"} or not isinstance(it.get("path"), str):
            raise PublishRefused("每个文件须为路径或 {path, op?}", en="each file must be a path or {path, op?}")
        op = it.get("op") or "write"
        if op not in OPS:
            raise PublishRefused(f"op 只能是 {OPS}（除删除外不用写）", en=f"op must be one of {OPS} (omit it unless deleting)")
        rel = where.admit(it["path"])          # normalise (scope prefix) + refuse what would add a layer
        if rel in seen:
            raise PublishRefused(f"路径重复：{it['path']!r}", en=f"duplicate path: {it['path']!r}")
        seen.add(rel)
        items.append((rel, op))
    return where, items


def _snapshot_group(scope, prefix, where, items, store_fd, max_bytes):
    """这一组的**快照阶段**：读文件、算摘要、把字节写进成果库的临时对象。

    返回 `(out, skipped, temps)`；**不提交**（提交由 `_commit_temps` 做）——
    这样多个工作空间可以“全部成功才一起固化”（W77 / #24）。
    任何一步失败 ⇒ 这一组写的临时对象全部清掉（不留半份）。
    """
    scope_fd = os.open(scope, os.O_RDONLY | os.O_DIRECTORY)
    temps, out, skipped = [], [], []
    try:
        for path, op in items:
            ref = f"{prefix}/{path}"
            if op == "delete":
                if not _absent(scope_fd, path, where):
                    raise PublishRefused(f"{path!r} 还在；先在你的工作区里删掉它", en=f"{path!r} still exists; delete it in your workspace first")
                out.append({"ref": ref, "exact_version": "tombstone", "op": "delete"})
                continue
            if is_appledouble(path, _head(scope_fd, path, where)):
                skipped.append(path)
                continue
            digest, size, tmp = _snapshot(scope_fd, path, max_bytes, store_fd, where)
            if tmp is not None:
                temps.append((tmp, digest, size))
            # 不带 `size`：它以前被写进 change_set，却没有任何读者，反而参与 `sub_digest` 摘要
            # （同一个文件小改一下字节、长度恰好不变时，摘要是靠 `exact_version` 区分的，不是它）。
            # 真实大小要在读的时候问 `artifact.read` —— 那里的 `size` 是按字节现算的。
            out.append({"ref": ref, "exact_version": ART.version_name(digest), "op": op})
        if not out:
            raise PublishRefused("没有可发布的内容（只选中了影子文件）", en="nothing to publish (only shadow files were selected)")
        return out, skipped, temps
    except BaseException:
        _drop_temps(store_fd, temps)
        raise
    finally:
        os.close(scope_fd)


def prepare_change_sets(groups, *, artifact_root=None, max_bytes=MAX_BYTES):
    """Prepare a multi-workspace snapshot without publishing formal content-addressed blobs.

    Returns ``(change_set, skipped_shadow_paths, commit, close)``. The caller must pass
    ``commit`` to Core's authenticated submission transaction and call ``close`` in a
    ``finally`` block. Until Core invokes ``commit`` after its write-transaction checks, all
    result bytes remain temporary and ``close`` removes them on refusal or conflict.
    """
    store_fd = _open_store(artifact_root)
    pending = []
    closed = False
    committed = False
    try:
        out, skipped = [], []
        for scope_root, prefix, files in groups:
            where, items = _plan_group(scope_root, prefix, files)
            g_out, g_skipped, g_temps = _snapshot_group(scope_root, prefix, where, items,
                                                         store_fd, max_bytes)
            out.extend(g_out)
            skipped.extend(g_skipped)
            pending.extend(g_temps)
        if not out:
            raise PublishRefused("没有可发布的内容（只选中了影子文件）",
                                 en="nothing to publish (only shadow files were selected)")
    except BaseException:
        _drop_temps(store_fd, pending)
        if store_fd is not None:
            os.close(store_fd)
        raise

    def commit():
        nonlocal committed
        if closed:
            raise RuntimeError("prepared workspace publish is already closed")
        if not committed:
            _commit_temps(store_fd, pending)
            pending.clear()
            committed = True

    def close():
        nonlocal closed, store_fd
        if closed:
            return
        _drop_temps(store_fd, pending)
        pending.clear()
        if store_fd is not None:
            os.close(store_fd)
            store_fd = None
        closed = True

    return out, skipped, commit, close
