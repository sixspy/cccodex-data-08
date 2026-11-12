"""试看 / 收下 两段式入账的六段验证脚本。

每段对应用户口径里的一条验收：
1. 试看结束账本仍空白
2. 收下之后账本只含试看见过的编码
3. 目录里混进来的编码写不进账本
4. 试看未结束去点收下会被挡下
5. 已经收下再试看会被挡下
6. 空看去点收下会被挡下
"""

from app.db import get_db
from app.services.mock_catalog import CATALOGS


def login(client):
    return client.post(
        "/login",
        data={"username": "admin", "password": "123456"},
        follow_redirects=True,
    )


def create_job(client, name, seed):
    client.post(
        "/jobs",
        data={"name": name, "seed_path": seed},
        follow_redirects=True,
    )


def job_id(app):
    with app.app_context():
        return get_db().execute(
            "SELECT id FROM crawl_jobs ORDER BY id DESC LIMIT 1"
        ).fetchone()["id"]


def ledger_keys(app, jid):
    with app.app_context():
        return [
            row["item_key"]
            for row in get_db().execute(
                "SELECT item_key FROM crawl_records WHERE job_id = ? ORDER BY id",
                (jid,),
            ).fetchall()
        ]


def preview_keys(app, jid):
    with app.app_context():
        return [
            row["item_key"]
            for row in get_db().execute(
                "SELECT item_key FROM preview_records WHERE job_id = ? ORDER BY id",
                (jid,),
            ).fetchall()
        ]


def job_status(app, jid):
    with app.app_context():
        return get_db().execute(
            "SELECT status FROM crawl_jobs WHERE id = ?", (jid,)
        ).fetchone()["status"]


# ---- 第 1 段：试看结束，账本仍空白 ---------------------------------------
def test_1_preview_keeps_ledger_blank(app, client):
    login(client)
    create_job(client, "p1", "/mock/catalog/alpha")
    jid = job_id(app)

    r = client.post(f"/jobs/{jid}/preview", follow_redirects=True)
    body = r.get_data(as_text=True)

    # 试看区域标题固定写“尚未入账”，且白话点明试看不入账
    assert "尚未入账" in body
    assert "账本仍保持空白" in body
    # 试看只列编码与题名
    assert "编码（item_key）" in body
    assert "市政管线测绘记录表" in body
    assert "排水井巡查台账" in body
    # 账本一块明确是空的
    assert "账本空白" in body
    assert ledger_keys(app, jid) == []
    # 快照里有 2 条（alpha 三条源数据、一条重复标题被去重）
    assert len(preview_keys(app, jid)) == 2
    assert job_status(app, jid) == "previewed"


# ---- 第 2 段：收下后账本只含试看见过的编码 -------------------------------
def test_2_accept_writes_only_previewed_codes(app, client):
    login(client)
    create_job(client, "p2", "/mock/catalog/alpha")
    jid = job_id(app)

    client.post(f"/jobs/{jid}/preview", follow_redirects=True)
    seen = preview_keys(app, jid)
    assert seen and ledger_keys(app, jid) == []

    r = client.post(f"/jobs/{jid}/accept", follow_redirects=True)
    assert "收下完成" in r.get_data(as_text=True)

    assert ledger_keys(app, jid) == seen
    assert job_status(app, jid) == "done"
    assert "已入账" in r.get_data(as_text=True)


# ---- 第 3 段：试看后目录里混进来的编码写不进去 ---------------------------
def test_3_intruder_code_cannot_sneak_into_ledger(app, client):
    login(client)
    create_job(client, "p3", "/mock/catalog/alpha")
    jid = job_id(app)

    client.post(f"/jobs/{jid}/preview", follow_redirects=True)
    seen = preview_keys(app, jid)

    # 试看结束后，往源目录里偷偷塞一条新编码
    original = list(CATALOGS["/mock/catalog/alpha"])
    CATALOGS["/mock/catalog/alpha"] = original + [
        {"sku": "A-999", "title": "偷偷混入的新编码"}
    ]
    try:
        client.post(f"/jobs/{jid}/accept", follow_redirects=True)
    finally:
        CATALOGS["/mock/catalog/alpha"] = original

    got = ledger_keys(app, jid)
    assert got == seen                      # 与试看完全一致
    assert not any("偷偷混入的新编码" in k for k in got)
    assert len(got) == len(seen)


# ---- 第 4 段：试看未结束（没点过试看）就收下，前后台都挡下 ----------------
def test_4_accept_blocked_before_preview(app, client):
    login(client)
    create_job(client, "p4", "/mock/catalog/alpha")
    jid = job_id(app)

    # 页面上收下按钮是灰的
    page = client.get(f"/jobs/{jid}").get_data(as_text=True)
    assert 'class="btn-accept"' in page and "disabled" in page

    r = client.post(f"/jobs/{jid}/accept", follow_redirects=True)
    assert "不能收下" in r.get_data(as_text=True)
    assert ledger_keys(app, jid) == []
    assert job_status(app, jid) == "draft"


# ---- 第 5 段：已经收下过的任务，再点试看要被挡下 -------------------------
def test_5_repreview_blocked_after_accept(app, client):
    login(client)
    create_job(client, "p5", "/mock/catalog/alpha")
    jid = job_id(app)

    client.post(f"/jobs/{jid}/preview", follow_redirects=True)
    client.post(f"/jobs/{jid}/accept", follow_redirects=True)
    snapshot_before = preview_keys(app, jid)
    assert job_status(app, jid) == "done"

    r = client.post(f"/jobs/{jid}/preview", follow_redirects=True)
    assert "不能再次试看" in r.get_data(as_text=True)

    # 状态与试看快照都没被第二次试看改动，账本也没多账
    assert job_status(app, jid) == "done"
    assert preview_keys(app, jid) == snapshot_before
    assert len(ledger_keys(app, jid)) == len(snapshot_before)


# ---- 第 6 段：空看（试看一条都没有）时收下要被挡下 -----------------------
def test_6_empty_preview_accept_blocked(app, client):
    login(client)
    create_job(client, "p6", "/mock/catalog/gamma")
    jid = job_id(app)

    r = client.post(f"/jobs/{jid}/preview", follow_redirects=True)
    body = r.get_data(as_text=True)
    assert "这次是空看" in body
    assert preview_keys(app, jid) == []
    assert job_status(app, jid) == "previewed"
    # 空看时收下按钮同样是灰的
    assert 'class="btn-accept"' in body and "disabled" in body

    r2 = client.post(f"/jobs/{jid}/accept", follow_redirects=True)
    assert "不能收下" in r2.get_data(as_text=True)
    assert ledger_keys(app, jid) == []
    assert job_status(app, jid) == "previewed"
