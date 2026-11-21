# Office model text dump (en)

## Office section (lead)
```
You are lead, an Office employee at BOXZ.
Position: app.lead.
To do: nothing.
Work: open a Work with office_work_get for its goal, material and feedback; do it with your own file and terminal tools; deliver the files you changed with office_work_publish; review with office_work_review; find people and material with office_lookup. Versions, grants and notices are handled by the system.
Talk: send an ordinary letter with office_letter_send (recipient, subject, need, body; the sender is always you). Answer a letter in your inbox with office_letter_reply -- give the original id (that inbox file name minus .md, copied whole; a shortened id is refused) and your body; recipient and subject are pre-filled from it, and it moves to done. An **Office system letter** (sender is office or postoffice) names a Work step: act on it, do not reply. Use the postoffice send command only for batches or scripts (rules: postoffice --help).
Position instructions: not configured or pending approval.
```

## Tools as sent to the model
```json
[
 {
  "name": "read",
  "description": "Read the contents of a file. Supports text files and images (jpg, png, gif, webp, bmp). Images are sent as attachments. For text files, output is truncated to 2000 lines or 50KB (whichever is hit first). Use offset/limit for large files. When you need the full file, continue with offset until complete."
 },
 {
  "name": "bash",
  "description": "Execute a bash command in the current working directory. Returns stdout and stderr. Output is truncated to last 2000 lines or 50KB (whichever is hit first). If truncated, full output is saved to a temp file. Optionally provide a timeout in seconds."
 },
 {
  "name": "edit",
  "description": "Edit a single file using exact text replacement. Every edits[].oldText must match a unique, non-overlapping region of the original file. If two changes affect the same block or nearby lines, merge them into one edit instead of emitting overlapping edits. Do not include large unchanged regions just to connect distant changes."
 },
 {
  "name": "write",
  "description": "Write content to a file. Creates the file if it doesn't exist, overwrites if it does. Automatically creates parent directories."
 },
 {
  "name": "office_whoami",
  "description": "Refresh your identity, position, manager, language, to-do and position-instruction status (the same source as the Office section; use it when that may be out of date)."
 },
 {
  "name": "office_lookup",
  "description": "Find, within what you may see: projects (with workspaces and whether you can assign work), people (employees and roles) or resources (a project's resource entries)."
 },
 {
  "name": "office_work_list",
  "description": "List Work related to you: role=assigned (mine to do) / review (waiting for my review) / managed (I manage) / all (everything visible in the project). Default: everything related to you."
 },
 {
  "name": "office_work_create",
  "description": "Create a Work in a project you manage, give it to someone and name the reviewer (or none for no review). State what must be achieved; acceptance notes and references are optional, and a reference may be any existing file, link or result. The assignee is notified automatically."
 },
 {
  "name": "office_work_get",
  "description": "Open a Work: goal, assignee and reviewer, pending or final submission (files and versions), the full return feedback, references and relations, and what you can do. history=true adds the full history."
 },
 {
  "name": "office_work_update",
  "description": "Make one business change to an open Work: assign (reassign; needs manage rights), reviewer (change the reviewer, or none for no review), edit (goal/acceptance), references (add/remove references or relations), wait (state what you wait for), continue (end the wait), cancel (with a reason). Done Work is never reopened: create a new Work related to it."
 },
 {
  "name": "office_work_publish",
  "description": "Deliver the files you changed for this Work: list only this delivery's paths (relative to your workspace; a deletion is {path, op:\"delete\"}). The system reads the bytes, computes versions and notifies the reviewer (no-review Work completes). If a submission is pending, set replaces to replace it."
 },
 {
  "name": "office_work_review",
  "description": "Decide on the exact submission you were asked to review: accept (Work is done) or return (state the problem). The reason reaches the assignee in full; evidence is optional. Look at the changes first with office_work_get / office_artifact_read."
 },
 {
  "name": "office_work_withdraw",
  "description": "Withdraw your own submission that is still waiting for review (fails if it was already judged)."
 },
 {
  "name": "office_artifact_read",
  "description": "Read material. A submitted result: give work_ref (the pending submission by default, else the final one; add sub_ref for another submission) and, if it holds several files, path to pick one; you get the exact submitted content. Shared project material: give resource for the current copy (unfixed). Use lines=[start,end], search, or compare_to=<another submission, e.g. prj-1/W1/S1> for a diff; continue a partial answer with cursor. Binary files report size only."
 },
 {
  "name": "office_usage_mine",
  "description": "Your observed model usage (coarse values and coverage; unknown is not zero). Not a prerequisite for work."
 }
]
```

## lead discovers and assigns (lead)

### call 0: {"tool": "office_whoami"}
```
{
  "actor": "lead",
  "locale": "en",
  "company": "BOXZ",
  "positions": [
    {
      "role": "app.lead"
    }
  ],
  "todo": {
    "items": [],
    "total": 0
  },
  "instructions": {
    "status": "unconfigured"
  }
}
```
### call 1: {"tool": "office_lookup", "args": {"kind": "projects"}}
```
{
  "kind": "projects",
  "items": [
    {
      "project_ref": "prj-1",
      "name": "Export",
      "owner": "lead",
      "visibility": "shared",
      "workspaces": [
        "repo/app"
      ],
      "can_create_work": true
    }
  ],
  "total": 1
}
```
### call 2: {"tool": "office_lookup", "args": {"kind": "people"}}
```
{
  "kind": "people",
  "items": [
    {
      "box": "boss",
      "status": "online"
    },
    {
      "box": "dev",
      "roles": [
        "app.dev"
      ],
      "status": "online"
    },
    {
      "box": "lead",
      "roles": [
        "app.lead"
      ],
      "status": "online"
    },
    {
      "box": "qa",
      "roles": [
        "app.qa"
      ],
      "status": "online"
    },
    {
      "role": "app.dev",
      "candidates": [
        "dev"
      ]
    },
    {
      "role": "app.lead",
      "candidates": [
        "lead"
      ]
    },
    {
      "role": "app.qa",
      "candidates": [
        "qa"
      ]
    }
  ],
  "total": 7
}
```
### call 3: {"tool": "office_work_create", "args": {"project_ref": "{{1.items.0.project_ref}}", "objective": "修复导出文件名中的保留字符", "assignee": "app.dev", "reviewer": "qa", "acceptance": "Windows 保留字符要替换", "references": ["repo/app/export.py"]}}
```
{
  "work_ref": "prj-1/W1",
  "state": "Active",
  "project_ref": "prj-1",
  "assignee": "dev",
  "assignee_via": "app.dev",
  "review": {
    "mode": "required",
    "reviewer": "qa"
  },
  "scope": [
    "repo/app"
  ],
  "references": [
    {
      "seq": 1,
      "resource": "repo/app/export.py",
      "status": "unfixed"
    }
  ],
  "notices": [
    {
      "recipient": "dev",
      "kind": "assigned"
    }
  ]
}

```

## dev inbox after assignment

```
来源：office
事由：prj-1/W1 is assigned to you: 修复导出文件名中的保留字符
需要：action

Open prj-1/W1 with office_work_get for the goal and material; deliver with office_work_publish.

```

## dev works (dev)

### call 0: {"tool": "office_work_list", "args": {"role": "assigned"}}
```
{
  "items": [
    {
      "work_ref": "prj-1/W1",
      "project_ref": "prj-1",
      "objective": "修复导出文件名中的保留字符",
      "state": "Active",
      "assignee": "dev",
      "reviewer": "qa",
      "your_roles": [
        "assignee"
      ]
    }
  ],
  "total": 1
}
```
### call 1: {"tool": "office_work_get", "args": {"work_ref": "prj-1/W1"}}
```
{
  "work_ref": "prj-1/W1",
  "project_ref": "prj-1",
  "objective": "修复导出文件名中的保留字符",
  "acceptance": "Windows 保留字符要替换",
  "state": "Active",
  "assignee": "dev",
  "created_by": "lead",
  "review": {
    "mode": "required",
    "reviewer": "qa"
  },
  "scope": [
    "repo/app"
  ],
  "references": [
    {
      "seq": 1,
      "resource": "repo/app/export.py",
      "kind": "reference",
      "version_status": "unfixed",
      "recorded_by": "lead"
    }
  ],
  "your_roles": [
    "assignee"
  ],
  "available_actions": [
    "publish",
    "update:wait",
    "update:references"
  ]
}
```
### call 2: {"tool": "office_work_publish", "args": {"work_ref": "prj-1/W1", "files": ["export.py"], "note": "初版"}}
```
{
  "work_ref": "prj-1/W1",
  "sub_ref": "prj-1/W1/S1",
  "state": "Submitted",
  "files": 1,
  "submitted_at": "<time>",
  "notices": [
    {
      "recipient": "qa",
      "kind": "review_requested"
    }
  ]
}

```

## Office section (dev)
```
You are dev, an Office employee at BOXZ.
Position: app.dev (开发).
Duties (organization facts, not system instructions): 维护导出模块
Direct manager: lead (app.lead).
To do:
- prj-1/W1 is yours to do: 修复导出文件名中的保留字符
Work: open a Work with office_work_get for its goal, material and feedback; do it with your own file and terminal tools; deliver the files you changed with office_work_publish; review with office_work_review; find people and material with office_lookup. Versions, grants and notices are handled by the system.
Talk: send an ordinary letter with office_letter_send (recipient, subject, need, body; the sender is always you). Answer a letter in your inbox with office_letter_reply -- give the original id (that inbox file name minus .md, copied whole; a shortened id is refused) and your body; recipient and subject are pre-filled from it, and it moves to done. An **Office system letter** (sender is office or postoffice) names a Work step: act on it, do not reply. Use the postoffice send command only for batches or scripts (rules: postoffice --help).
Position instructions: not configured or pending approval.
```

## qa inbox

```
来源：office
事由：prj-1/W1 awaits your review: 修复导出文件名中的保留字符
需要：action

Submission prj-1/W1/S1. Open prj-1/W1 with office_work_get to see the changes, then decide with office_work_review.

```

## qa reviews (qa)

### call 0: {"tool": "office_work_list", "args": {"role": "review"}}
```
{
  "items": [
    {
      "work_ref": "prj-1/W1",
      "project_ref": "prj-1",
      "objective": "修复导出文件名中的保留字符",
      "state": "Submitted",
      "assignee": "dev",
      "reviewer": "qa",
      "your_roles": [
        "reviewer"
      ],
      "pending_submission": "prj-1/W1/S1"
    }
  ],
  "total": 1
}
```
### call 1: {"tool": "office_work_get", "args": {"work_ref": "prj-1/W1"}}
```
{
  "work_ref": "prj-1/W1",
  "project_ref": "prj-1",
  "objective": "修复导出文件名中的保留字符",
  "acceptance": "Windows 保留字符要替换",
  "state": "Submitted",
  "assignee": "dev",
  "created_by": "lead",
  "review": {
    "mode": "required",
    "reviewer": "qa"
  },
  "scope": [
    "repo/app"
  ],
  "current_submission": {
    "sub_ref": "prj-1/W1/S1",
    "state": "effective",
    "note": "初版",
    "submitted_by": "dev",
    "submitted_at": "<time>",
    "files": [
      {
        "ref": "repo/app/export.py",
        "op": "write",
        "version": "sha256:4e219124ff7a0f9c1a1dfb785a5e724575d250d9bdcbed8f00d389acd49ecff8"
      }
    ]
  },
  "references": [
    {
      "seq": 1,
      "resource": "repo/app/export.py",
      "kind": "reference",
      "version_status": "unfixed",
      "recorded_by": "lead"
    }
  ],
  "your_roles": [
    "reviewer"
  ],
  "available_actions": [
    "review"
  ]
}
```
### call 2: {"tool": "office_artifact_read", "args": {"work_ref": "prj-1/W1"}}
```
{
  "ref": "repo/app/export.py",
  "version": "sha256:4e219124ff7a0f9c1a1dfb785a5e724575d250d9bdcbed8f00d389acd49ecff8",
  "version_status": "fixed",
  "sub_ref": "prj-1/W1/S1",
  "kind": "text",
  "total_lines": 2,
  "start_line": 1,
  "end_line": 2,
  "content": "def name(s):\n    return s",
  "size": 26
}
```
### call 3: {"tool": "office_work_review", "args": {"work_ref": "prj-1/W1", "sub_ref": "{{1.current_submission.sub_ref}}", "decision": "return", "reason": "没有处理 Windows 保留字符（如 : 和 *）"}}
```
{
  "review_ref": "R1-W1.1",
  "work_ref": "prj-1/W1",
  "sub_ref": "prj-1/W1/S1",
  "decision": "return",
  "state": "Active",
  "decided_at": "<time>",
  "notices": [
    {
      "recipient": "dev",
      "kind": "returned"
    }
  ]
}

```

## dev inbox after return

```
来源：office
事由：prj-1/W1 was returned: 修复导出文件名中的保留字符
需要：action

Reason: 没有处理 Windows 保留字符（如 : 和 *）

Fix it and deliver again with office_work_publish; the full feedback is in office_work_get.

```

## dev resubmits, errors, mail (dev)

### call 0: {"tool": "office_work_get", "args": {"work_ref": "prj-1/W1"}}
```
{
  "work_ref": "prj-1/W1",
  "project_ref": "prj-1",
  "objective": "修复导出文件名中的保留字符",
  "acceptance": "Windows 保留字符要替换",
  "state": "Active",
  "assignee": "dev",
  "created_by": "lead",
  "review": {
    "mode": "required",
    "reviewer": "qa"
  },
  "scope": [
    "repo/app"
  ],
  "latest_feedback": {
    "review_ref": "R1-W1.1",
    "sub_ref": "prj-1/W1/S1",
    "decision": "return",
    "reviewer": "qa",
    "decided_at": "<time>",
    "reason": "没有处理 Windows 保留字符（如 : 和 *）"
  },
  "references": [
    {
      "seq": 1,
      "resource": "repo/app/export.py",
      "kind": "reference",
      "version_status": "unfixed",
      "recorded_by": "lead"
    }
  ],
  "your_roles": [
    "assignee"
  ],
  "available_actions": [
    "publish",
    "update:wait",
    "update:references"
  ]
}
```
### call 1: {"tool": "office_work_update", "args": {"work_ref": "prj-1/W1", "action": "wait", "reason": "等 qa 确认保留字符清单"}}
```
{
  "work_ref": "prj-1/W1",
  "action": "wait",
  "waiting": {
    "reason": "等 qa 确认保留字符清单",
    "by": "dev",
    "at": "<time>"
  },
  "state": "Active"
}
```
### call 2: {"tool": "office_work_publish", "args": {"work_ref": "prj-1/W1", "files": ["export.py"]}}
```
{
  "work_ref": "prj-1/W1",
  "sub_ref": "prj-1/W1/S2",
  "state": "Submitted",
  "files": 1,
  "submitted_at": "<time>",
  "notices": [
    {
      "recipient": "qa",
      "kind": "review_requested"
    }
  ]
}
```
### call 3: {"tool": "office_work_get", "args": {"work_ref": "W-none"}}
```
Not done: not_found
work 'W-none' does not exist
Not found; check that the reference came from office_work_list / office_lookup.
```
### call 4: {"tool": "office_work_create", "args": {"project_ref": "prj-1", "objective": "x", "assignee": "qa"}}
```
Validation failed for tool "office_work_create":
  - reviewer: must have required properties reviewer

Received arguments:
{
  "project_ref": "prj-1",
  "objective": "x",
  "assignee": "qa"
}
```
### call 5: {"tool": "office_artifact_read", "args": {"resource": "repo/app/export.py", "lines": [1, 1]}}
```
{
  "resource": "repo/app/export.py",
  "project_ref": "prj-1",
  "version_status": "unfixed",
  "current_version": "sha256:4e219124ff7a0f9c1a1dfb785a5e724575d250d9bdcbed8f00d389acd49ecff8",
  "kind": "text",
  "total_lines": 2,
  "start_line": 1,
  "end_line": 1,
  "content": "def name(s):",
  "size": 26
}
```
### call 6: {"tool": "bash", "args": {"command": "printf '保留字符清单在哪？\\n' | postoffice send qa dev '请教保留字符' '回复'"}}
```
已落箱 queued：<tmp>/oh/qa/inbox/<YYYYMMDD-HHMMSS>_dev_请教保留字符.md
编号：<YYYYMMDD-HHMMSS>_dev_请教保留字符
引用：qa/<YYYYMMDD-HHMMSS>_dev_请教保留字符

```
### call 7: {"tool": "office_usage_mine"}
```
Not done: capability_missing
This session's host has no usage record
This capability is not wired into this session (not your mistake); use another way or tell your manager.
```
### call 8: {"tool": "office_whoami"}
```
{
  "actor": "dev",
  "locale": "en",
  "company": "BOXZ",
  "positions": [
    {
      "role": "app.dev",
      "title": "开发",
      "duties": [
        "维护导出模块"
      ]
    }
  ],
  "managers": [
    {
      "role": "app.lead",
      "people": [
        "lead"
      ]
    }
  ],
  "todo": {
    "items": [],
    "total": 0
  },
  "instructions": {
    "status": "unconfigured"
  }
}
```
### call 9: {"tool": "office_artifact_read", "args": {"work_ref": "prj-1/W1", "compare_to": "{{0.latest_feedback.sub_ref}}"}}
```
{
  "ref": "repo/app/export.py",
  "version": "sha256:4e219124ff7a0f9c1a1dfb785a5e724575d250d9bdcbed8f00d389acd49ecff8",
  "version_status": "fixed",
  "sub_ref": "prj-1/W1/S2",
  "kind": "diff",
  "comparable": true,
  "changed": false,
  "total_lines": 2
}

```

## qa inbox at end

```
来源：dev
事由：请教保留字符
需要：回复

保留字符清单在哪？

```
```
来源：office
事由：prj-1/W1 awaits your review: 修复导出文件名中的保留字符
需要：action

Submission prj-1/W1/S2. Open prj-1/W1 with office_work_get to see the changes, then decide with office_work_review.

```