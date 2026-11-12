import json
import sqlite3

from flask import current_app

from app.db import db_transaction, get_db
from app.services.dedup import normalize_item_key
from app.services.mock_catalog import CATALOGS, parse_catalog_html, render_catalog_page


def _get_job(db, job_id: int):
    return db.execute(
        "SELECT * FROM crawl_jobs WHERE id = ?", (job_id,)
    ).fetchone()


def _collect_seed_items(seed: str) -> list[dict]:
    """解析 mock 目录，返回目录里的全部原始条目（含同标题重复项）。"""
    html = render_catalog_page(seed)
    if isinstance(html, tuple):
        html = html[0]
    return parse_catalog_html(html, seed)


def preview_job(job_id: int) -> dict:
    """试看：只把即将收下的编码（item_key）和题名存进快照，账本保持空白。

    同任务内同 item_key 的重复条目在试看阶段就折成一条，重复数计入 deduped，
    快照与后续账本里都不会出现重复编码。
    """
    db = get_db()
    job = _get_job(db, job_id)
    if job is None:
        raise ValueError("任务不存在")
    if job["status"] == "done":
        raise ValueError("任务已收下入账，不能再次试看")

    seed = job["seed_path"]
    if seed not in CATALOGS:
        with db_transaction() as tx:
            tx.execute(
                "UPDATE crawl_jobs SET status = 'failed', finished_at = datetime('now') WHERE id = ?",
                (job_id,),
            )
        return {
            "status": "failed",
            "reason": "unknown seed",
            "previewed": 0,
            "deduped": 0,
            "total_source": 0,
        }

    items = _collect_seed_items(seed)
    rows = []
    seen: set[str] = set()
    deduped = 0
    for it in items:
        key = normalize_item_key(seed, it["title"])
        if key in seen:
            deduped += 1
            continue
        seen.add(key)
        rows.append(
            (
                job_id,
                key,
                it["title"],
                json.dumps(it, ensure_ascii=False),
            )
        )
    with db_transaction() as tx:
        # 试看可重放（仅未入账前）：整体替换快照，始终以最近一次试看为准
        tx.execute("DELETE FROM preview_records WHERE job_id = ?", (job_id,))
        if rows:
            tx.executemany(
                """
                INSERT INTO preview_records (job_id, item_key, title, payload_json)
                VALUES (?, ?, ?, ?)
                """,
                rows,
            )
        tx.execute(
            "UPDATE crawl_jobs SET status = 'previewed', finished_at = NULL WHERE id = ?",
            (job_id,),
        )

    current_app.logger.info(
        "job %s previewed items=%s deduped=%s (ledger untouched)",
        job_id,
        len(rows),
        deduped,
    )
    return {
        "status": "previewed",
        "previewed": len(rows),
        "deduped": deduped,
        "total_source": len(items),
    }


def accept_job(job_id: int) -> dict:
    """收下：只把试看快照里出现过的编码写进账本，目录里多出来的编码进不来。"""
    db = get_db()
    job = _get_job(db, job_id)
    if job is None:
        raise ValueError("任务不存在")
    if job["status"] == "done":
        raise ValueError("任务已收下入账，不能重复收下")

    previews = db.execute(
        "SELECT item_key, title, payload_json FROM preview_records WHERE job_id = ? ORDER BY id",
        (job_id,),
    ).fetchall()
    if not previews:
        raise ValueError("试看尚未结束或本次是空看，不能收下")

    inserted = 0
    skipped_dup = 0
    with db_transaction() as tx:
        for p in previews:
            try:
                tx.execute(
                    """
                    INSERT INTO crawl_records (job_id, item_key, title, payload_json)
                    VALUES (?, ?, ?, ?)
                    """,
                    (job_id, p["item_key"], p["title"], p["payload_json"]),
                )
                inserted += 1
            except sqlite3.IntegrityError:
                skipped_dup += 1
        tx.execute(
            "UPDATE crawl_jobs SET status = 'done', finished_at = datetime('now') WHERE id = ?",
            (job_id,),
        )

    current_app.logger.info(
        "job %s accepted inserted=%s skipped_dup=%s",
        job_id,
        inserted,
        skipped_dup,
    )
    return {
        "status": "done",
        "inserted": inserted,
        "skipped_dup": skipped_dup,
        "total_source": len(previews),
    }


def run_job(job_id: int) -> dict:
    """兼容旧入口：试看后立即收下（等价于原一步采集，保持服务层契约）。"""
    preview = preview_job(job_id)
    if preview.get("status") == "failed":
        return {
            "status": "failed",
            "reason": preview.get("reason"),
            "inserted": 0,
            "skipped_dup": 0,
        }
    accepted = accept_job(job_id)
    # 试看阶段折掉的同编码重复项与入账阶段的约束去重，统一算作跳过重复
    accepted["skipped_dup"] += preview["deduped"]
    accepted["total_source"] = preview["total_source"]
    return accepted
