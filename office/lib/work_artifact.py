"""成果路径的那两个真判据：`change_set` 的常量与解析、越界判据 `scope_contains`。

这一层**不新增表**，也不自己再投影一遍既有事实：

  · **这一版交付了哪些文件** —— 一次提交的 `change_set` 就是那个清单（每条形如
    `{ref, exact_version, op}`，`op` 只在删除时是 `delete`）。真入口由 `work.get` 的提交摘要
    回答 —— 解析只在 `work_result.change_set_of` 一处：`work.get` 的 `current_submission.files`
    就是它，Core 的引用集合也取这一个。
  · **旧成果被哪项 Work 更新**（"A 已被 W-2 更新"）—— `work_relations` 的 `modifies` 行。
  · **越界** —— `scope_contains`：一份成果的 `ref` 是否落在 Work 的交付范围之下。

这里曾经还立着四条投影（`artifact_tree`、`result_lineage`、`superseded_by`、`modifies_of`）
和一条拼登记根的算法（`workspace_root_of`）。它们回答的三个问句上面都已经有**既有事实**，
而它们自己**零产品调用方**（只有我的探针在读）—— 与先前退掉的 `artifact_paths` 一样是
"平行投影"，会与真出处各自漂，整段退掉。

`change_set.ref` 是**相对登记工作区根**的路径：登记工作区可以是仓库的子目录，也可以是另一个
仓库，所以绝不能拿仓库根去拼（W395 实测会拼出 `office/mcode/office/mcode/foo`）。谁来提供
登记根（端点、适配层）不在这层决定。
"""
from work_result import DELETE, WRITE, change_set_of     # noqa: F401  （Core 从这里取常量与解析）


def scope_contains(scope_refs, ref):
    """一项成果（`ref`）是否落在这些交付范围里 —— **唯一**的越界判据。

    判据只有一条：`ref` 自己（或它的某一层父目录）等于某个范围。**没有**第二个坐标 ——
    曾经还有"登记根落在某个范围之下"，而登记根是从 `scope[0]` 反查来的、就是那个范围自己，
    于是对任何 ref 恒真（单范围时整条判据形同虚设：只准交 `docs` 的 Work 收下了 `src/x.py`）。
    发布出来的 `ref` 永远带着自己的范围前缀（`r/publish` 拼的是 `f"{prefix}/{path}"`），
    前缀这一条就够。

    从前判据里还有一条"`rel` 的某一层父目录等于范围"。它**恒被上一条蕴含**（父目录等于 `s`
    ⇒ `rel` 就是 `s` 加一段尾巴 ⇒ `startswith(s + "/")`）：删掉前后 20,140 组输入
    （140 定点 + 20,000 随机）逐组同答，删。
    """
    rel = (ref or "").lstrip("/")
    for sc in scope_refs or ():
        s = (sc or "").strip("/")
        if s and (rel == s or rel.startswith(s + "/")):
            return True
    return False
