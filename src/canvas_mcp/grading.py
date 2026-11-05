"""Grading-config state: load/save, patch validation & application, history.

All writes land under ``data/courses/<slug>/`` — this module never touches
Canvas. The LLM only ever produces a *patch* (see :mod:`canvas_mcp.llm`); every
field below is validated and applied here, in code. Scores are never computed
here either — that is :mod:`canvas_mcp.progress`.

Files per course:
  grading.draft.json    extracted draft (unconfirmed, may be overwritten)
  grading.json          confirmed config (never overwritten by extraction)
  grading.history.jsonl one line per confirmed change (with full snapshot)
  pending/<id>.json     a proposed patch awaiting confirmation (24h TTL)
"""

from __future__ import annotations

import copy
import json
import re
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from . import progress as _progress

REPO_ROOT = Path(__file__).resolve().parents[2]
WEIGHT_TOLERANCE = 0.01
PENDING_TTL = timedelta(hours=24)

VALID_OPS = {
    "add_item", "update_item", "remove_item",
    "set_manual_score", "clear_manual_score",
    "map_assignment", "unmap_assignment", "set_grade_scale",
}
ITEM_TYPES = {"assignment", "exam", "project", "quiz", "participation", "other"}


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

def course_slug(course_code: str) -> str:
    """A filesystem-safe directory name derived from the Canvas course code."""
    return re.sub(r"[^0-9A-Za-z]+", "-", course_code or "course").strip("-") or "course"


def data_root(base: Path | str | None = None) -> Path:
    return Path(base) if base else REPO_ROOT / "data"


def course_dir(course_code: str, base: Path | str | None = None) -> Path:
    return data_root(base) / "courses" / course_slug(course_code)


def _read_json(path: Path) -> Any | None:
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")


def load_draft(course_code, base=None) -> dict | None:
    return _read_json(course_dir(course_code, base) / "grading.draft.json")


def save_draft(course_code, draft, base=None) -> Path:
    path = course_dir(course_code, base) / "grading.draft.json"
    _write_json(path, draft)
    return path


def load_grading(course_code, base=None) -> dict | None:
    return _read_json(course_dir(course_code, base) / "grading.json")


def save_grading(course_code, grading, base=None) -> Path:
    path = course_dir(course_code, base) / "grading.json"
    _write_json(path, grading)
    return path


# ---------------------------------------------------------------------------
# History (append-only log of confirmed changes, with snapshots for undo)
# ---------------------------------------------------------------------------

def _history_path(course_code, base=None) -> Path:
    return course_dir(course_code, base) / "grading.history.jsonl"


def read_history(course_code, base=None) -> list[dict]:
    path = _history_path(course_code, base)
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def append_history(course_code, entry, base=None) -> None:
    path = _history_path(course_code, base)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")


def _write_history(course_code, entries, base=None) -> None:
    path = _history_path(course_code, base)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(e, ensure_ascii=False) + "\n" for e in entries),
        encoding="utf-8",
    )


def record_change(course_code, grading, instruction, patch, now, base=None) -> None:
    """Persist grading.json and append a history snapshot."""
    save_grading(course_code, grading, base)
    append_history(course_code, {
        "at": now.isoformat(timespec="seconds"),
        "instruction": instruction,
        "patch": patch,
        "grading_after": grading,
    }, base)


def undo_last(course_code, base=None) -> dict | None:
    """Revert the most recent change; return the restored grading (or None if
    there is nothing to undo beyond the initial confirmation)."""
    history = read_history(course_code, base)
    if len(history) <= 1:
        return None  # only the baseline (or nothing) remains
    history.pop()
    restored = history[-1]["grading_after"]
    save_grading(course_code, restored, base)
    _write_history(course_code, history, base)
    return restored


def restore_to(course_code, index, base=None) -> dict | None:
    """Roll back to history entry `index` (0 = baseline), truncating later ones."""
    history = read_history(course_code, base)
    if not (0 <= index < len(history)):
        return None
    history = history[: index + 1]
    restored = history[-1]["grading_after"]
    save_grading(course_code, restored, base)
    _write_history(course_code, history, base)
    return restored


# ---------------------------------------------------------------------------
# Pending patches (proposed via MCP, awaiting confirmation)
# ---------------------------------------------------------------------------

def _pending_dir(course_code, base=None) -> Path:
    return course_dir(course_code, base) / "pending"


def new_patch_id() -> str:
    return uuid.uuid4().hex[:12]


def save_pending(course_code, payload, base=None) -> Path:
    path = _pending_dir(course_code, base) / f"{payload['patch_id']}.json"
    _write_json(path, payload)
    return path


def load_pending(patch_id, course_code=None, base=None) -> dict | None:
    if course_code is not None:
        return _read_json(_pending_dir(course_code, base) / f"{patch_id}.json")
    # Search every course's pending dir.
    root = data_root(base) / "courses"
    if not root.is_dir():
        return None
    for d in root.iterdir():
        candidate = d / "pending" / f"{patch_id}.json"
        if candidate.is_file():
            return _read_json(candidate)
    return None


def delete_pending(course_code, patch_id, base=None) -> None:
    path = _pending_dir(course_code, base) / f"{patch_id}.json"
    if path.is_file():
        path.unlink()


def is_pending_expired(payload, now) -> bool:
    created = payload.get("created_at")
    if not created:
        return True
    try:
        ts = datetime.fromisoformat(created)
    except ValueError:
        return True
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return now - ts > PENDING_TTL


# ---------------------------------------------------------------------------
# Patch validation & application
# ---------------------------------------------------------------------------

def _norm_rules(rules: dict | None) -> dict:
    rules = rules or {}
    return {
        "drop_lowest": int(rules.get("drop_lowest") or 0),
        "drop_highest": int(rules.get("drop_highest") or 0),
        "never_drop": list(rules.get("never_drop") or []),
    }


def _find_item(grading: dict, name: str) -> dict | None:
    for it in grading.get("items") or []:
        if it.get("name") == name:
            return it
    return None


def _weight_sum(grading: dict) -> float:
    return sum(float(it.get("weight") or 0) for it in grading.get("items") or [])


_TYPE_KEYWORDS = [
    (("final", "midterm", "exam", "test", "考试", "期中", "期末"), "exam"),
    (("quiz", "小测"), "quiz"),
    (("project", "项目", "大作业"), "project"),
    (("participation", "attendance", "参与", "出勤"), "participation"),
    (("assignment", "homework", "hw", "exercise", "problem", "作业", "习题"), "assignment"),
]


def guess_item_type(name: str) -> str:
    low = (name or "").lower()
    for keywords, itype in _TYPE_KEYWORDS:
        if any(k in low for k in keywords):
            return itype
    return "other"


def draft_from_assignment_groups(
    course: dict[str, Any], groups: list[dict[str, Any]]
) -> dict[str, Any]:
    """Build a high-confidence draft directly from weighted assignment_groups.

    No LLM involved — weights, drop rules and the assignment mapping all come
    straight from Canvas."""
    items: list[dict[str, Any]] = []
    mapping: dict[str, list] = {}
    warnings: list[str] = []
    for g in groups:
        name = g.get("name")
        weight = float(g.get("group_weight") or 0)
        rules = g.get("rules") or {}
        items.append({
            "name": name,
            "type": guess_item_type(name),
            "weight": weight,
            "rules": _norm_rules(rules),
            "source_quote": f"assignment group “{name}” group_weight={weight}",
        })
        mapping[name] = [
            a.get("id") for a in g.get("assignments") or [] if a.get("id") is not None
        ]
    total = sum(i["weight"] for i in items)
    if abs(total - 100) > WEIGHT_TOLERANCE:
        warnings.append(f"assignment_group 权重之和为 {round(total, 2)}%，不等于 100%。")
    return {
        "course_id": course.get("id"),
        "course_code": course.get("course_code"),
        "course_name": course.get("name"),
        "graded": total > 0,
        "items": items,
        "mapping": mapping,
        "manual_scores": {},
        "source": "assignment_groups",
        "confidence": "high",
        "warnings": warnings,
    }


def draft_from_llm(
    course: dict[str, Any],
    llm_result: dict[str, Any],
    source: str,
    canvas_assignments: list[dict[str, Any]],
) -> dict[str, Any]:
    """Assemble a draft from an LLM extraction result. Mapping guesses are kept
    only when they reference real Canvas assignment ids."""
    valid_ids = {a.get("id") for a in canvas_assignments}
    items = []
    for it in llm_result.get("items") or []:
        items.append({
            "name": it.get("name"),
            "type": it.get("type") or guess_item_type(it.get("name") or ""),
            "weight": float(it.get("weight") or 0),
            "rules": _norm_rules(it.get("rules")),
            "source_quote": it.get("source_quote"),
        })
    mapping: dict[str, list] = {}
    for guess in llm_result.get("mapping_guesses") or []:
        ids = [i for i in guess.get("assignment_ids") or [] if i in valid_ids]
        if ids:
            mapping[guess.get("item_name")] = ids
    warnings = list(llm_result.get("warnings") or [])
    total = sum(i["weight"] for i in items)
    if items and abs(total - 100) > WEIGHT_TOLERANCE:
        warnings.append(f"提取出的权重之和为 {round(total, 2)}%，不等于 100%。")
    draft = {
        "course_id": course.get("id"),
        "course_code": course.get("course_code"),
        "course_name": course.get("name"),
        "graded": bool(items),
        "items": items,
        "mapping": mapping,
        "manual_scores": {},
        "source": source,
        "confidence": llm_result.get("confidence") or "low",
        "warnings": warnings,
    }
    if llm_result.get("grade_scale"):
        draft["grade_scale"] = llm_result["grade_scale"]
    return draft


class PatchError(Exception):
    """A patch that fails validation (carries a user-facing message)."""


def apply_patch(
    grading: dict,
    patch: dict,
    canvas_assignments: list[dict],
) -> tuple[dict, list[str]]:
    """Validate and apply a patch to a copy of `grading`.

    Returns ``(new_grading, warnings)``. Raises :class:`PatchError` with a clear
    message on any hard failure. Never auto-balances weights."""
    ops = patch.get("operations") or []
    if not ops:
        raise PatchError("补丁为空：没有可执行的操作。")

    canvas_ids = {a["id"] for a in canvas_assignments if a.get("id") is not None}
    g = copy.deepcopy(grading)
    g.setdefault("items", [])
    g.setdefault("mapping", {})
    g.setdefault("manual_scores", {})
    item_names = {it["name"] for it in g["items"]}
    weight_changed = False
    warnings: list[str] = []

    for op in ops:
        kind = op.get("op")
        if kind not in VALID_OPS:
            raise PatchError(f"未知操作 {kind!r}。")

        if kind == "add_item":
            name = op.get("name")
            if not name:
                raise PatchError("add_item 缺少 name。")
            if name in item_names:
                raise PatchError(f"项目 {name!r} 已存在，不能重复添加。")
            itype = op.get("type") or "other"
            if itype not in ITEM_TYPES:
                raise PatchError(f"未知类型 {itype!r}。可选：{', '.join(sorted(ITEM_TYPES))}。")
            g["items"].append({
                "name": name, "type": itype,
                "weight": float(op.get("weight") or 0),
                "rules": _norm_rules(op.get("rules")),
                "source_quote": op.get("source_text"),
            })
            item_names.add(name)
            weight_changed = True

        elif kind == "update_item":
            name = op.get("name")
            it = _find_item(g, name)
            if it is None:
                raise PatchError(f"项目 {name!r} 不存在，无法修改。")
            if "weight" in op and op["weight"] is not None:
                it["weight"] = float(op["weight"])
                weight_changed = True
            if op.get("type"):
                if op["type"] not in ITEM_TYPES:
                    raise PatchError(f"未知类型 {op['type']!r}。")
                it["type"] = op["type"]
            if op.get("rules") is not None:
                it["rules"] = _norm_rules(op["rules"])

        elif kind == "remove_item":
            name = op.get("name")
            if _find_item(g, name) is None:
                raise PatchError(f"项目 {name!r} 不存在，无法删除。")
            g["items"] = [it for it in g["items"] if it["name"] != name]
            g["mapping"].pop(name, None)
            g["manual_scores"].pop(name, None)
            item_names.discard(name)
            weight_changed = True

        elif kind == "map_assignment":
            name = op.get("item")
            aid = op.get("assignment_id")
            if _find_item(g, name) is None:
                raise PatchError(f"项目 {name!r} 不存在，无法关联作业。")
            if aid not in canvas_ids:
                raise PatchError(f"Canvas 作业 id {aid!r} 不存在，无法关联。")
            g["mapping"].setdefault(name, [])
            if aid not in g["mapping"][name]:
                g["mapping"][name].append(aid)

        elif kind == "unmap_assignment":
            name = op.get("item")
            aid = op.get("assignment_id")
            lst = g["mapping"].get(name) or []
            if aid not in lst:
                raise PatchError(f"项目 {name!r} 并未关联作业 {aid!r}。")
            g["mapping"][name] = [x for x in lst if x != aid]

        elif kind == "set_manual_score":
            key = op.get("key")
            score = op.get("score")
            out_of = op.get("out_of")
            if score is None or out_of is None:
                raise PatchError("set_manual_score 需要 score 和 out_of。")
            if out_of <= 0:
                raise PatchError("out_of 必须大于 0。")
            if score > out_of:
                raise PatchError(f"score {score} 不能大于 out_of {out_of}。")
            # key is either an existing item name or a real Canvas assignment id.
            if key not in item_names:
                aid = key
                if isinstance(aid, str) and aid.isdigit():
                    aid = int(aid)
                if aid not in canvas_ids:
                    raise PatchError(
                        f"{key!r} 既不是已有项目，也不是真实的 Canvas 作业 id。"
                    )
            g["manual_scores"][str(key)] = {"score": score, "out_of": out_of}

        elif kind == "clear_manual_score":
            key = str(op.get("key"))
            if key not in g["manual_scores"]:
                raise PatchError(f"没有 {key!r} 的手动成绩可清除。")
            g["manual_scores"].pop(key)

        elif kind == "set_grade_scale":
            scale = op.get("scale") or {}
            if not scale or not all(isinstance(v, (int, float)) for v in scale.values()):
                raise PatchError("grade_scale 必须是 {等级: 数字分数线}。")
            g["grade_scale"] = {k: float(v) for k, v in scale.items()}

    # Drop counts must stay below each item's assignment count.
    for it in g["items"]:
        rules = _norm_rules(it.get("rules"))
        drops = rules["drop_lowest"] + rules["drop_highest"]
        n = len(g["mapping"].get(it["name"]) or [])
        if drops and drops >= max(n, 1):
            raise PatchError(
                f"项目 {it['name']!r} 的 drop 数 {drops} 必须小于作业数 {n}。"
            )

    # Weights must total 100 whenever the patch changed them. No auto-balance.
    if weight_changed:
        total = _weight_sum(g)
        if abs(total - 100) > WEIGHT_TOLERANCE:
            delta = round(total - 100, 2)
            direction = "超出" if delta > 0 else "缺少"
            raise PatchError(
                f"修改后权重之和为 {round(total, 2)}%，不等于 100%（{direction} "
                f"{abs(delta)}%）。请指定从哪一项增减，我不会自动平衡。"
            )
    return g, warnings


# ---------------------------------------------------------------------------
# Impact preview
# ---------------------------------------------------------------------------

def progress_impact(
    before: dict | None, after: dict | None, target_label: str = "A"
) -> dict[str, Any]:
    """A compact before/after summary of the course-level numbers that matter."""
    def snap(p):
        if not p or p.get("status") != "ok":
            return None
        tgt = next((t for t in p.get("targets", []) if t["label"] == target_label), None)
        return {
            "floor": p.get("floor"),
            "ceiling": p.get("ceiling"),
            "graded_avg": p.get("graded_avg"),
            "need_pct": (tgt or {}).get("need_pct"),
            "target_state": (tgt or {}).get("state"),
        }
    return {"target": target_label, "before": snap(before), "after": snap(after)}


def item_diff(before: dict, after: dict) -> list[str]:
    """Human-readable lines describing item/weight/mapping changes."""
    lines: list[str] = []
    b_items = {it["name"]: it for it in before.get("items") or []}
    a_items = {it["name"]: it for it in after.get("items") or []}
    for name in a_items:
        if name not in b_items:
            lines.append(f"+ 新增项目 {name}（权重 {a_items[name].get('weight')}%）")
    for name in b_items:
        if name not in a_items:
            lines.append(f"- 删除项目 {name}")
    for name in a_items.keys() & b_items.keys():
        bw, aw = b_items[name].get("weight"), a_items[name].get("weight")
        if bw != aw:
            lines.append(f"~ {name} 权重 {bw}% → {aw}%")
        if (b_items[name].get("rules") or {}) != (a_items[name].get("rules") or {}):
            lines.append(f"~ {name} 规则 {b_items[name].get('rules')} → {a_items[name].get('rules')}")
    b_map, a_map = before.get("mapping") or {}, after.get("mapping") or {}
    for name in a_map.keys() | b_map.keys():
        if (b_map.get(name) or []) != (a_map.get(name) or []):
            lines.append(f"~ {name} 关联作业 {b_map.get(name) or []} → {a_map.get(name) or []}")
    b_ms, a_ms = before.get("manual_scores") or {}, after.get("manual_scores") or {}
    for key in a_ms.keys() | b_ms.keys():
        if b_ms.get(key) != a_ms.get(key):
            lines.append(f"~ 手动成绩 {key}: {b_ms.get(key)} → {a_ms.get(key)}")
    if (before.get("grade_scale") or {}) != (after.get("grade_scale") or {}) and after.get("grade_scale"):
        lines.append(f"~ 等级线 → {after.get('grade_scale')}")
    return lines
