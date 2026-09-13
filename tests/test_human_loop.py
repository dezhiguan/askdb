"""六档「需要人工介入」状态的闭环（2026-09-11）。

**这个文件盯的是"卡住的任务出得去吗"**，不是某个接口的形状。

2026-09-11 盘点发现九档任务态里只有 DONE / BLOCK 是通的，其余六档各自断在
不同位置：有的没人有权处理（线上一个 SYS_ADMIN 账号都没有）、有的没有入口
（审批页被撤下）、有的按钮是假的（补充表单写死、填的内容在调用处被丢掉）、
有的只进不出（运维那一档压根没有写端点）。当时线上积压 61 待审批、25 待复核、
1 待运维、294 等待补充、8 条卡了十几小时的僵尸线程。

所以这里的每条用例都对应一条**真实断过的链路**，断法一并写在用例里 ——
它们是回归护栏，不是覆盖率填充。三条队列（审批 / 复核 / 运维）各自有独立的
存储与决策人，用例也照这个边界分组。
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from askdb import approvals, audit, auth, identity, ops, server

SECRET = "s" * 40


def _now(offset_s: int = 0) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=offset_s)).isoformat()


def _rec(trace_id: str, thread_id: str, *, user: str, rejected: str | None,
         question: str = "库里有多少文档", ts: str | None = None) -> dict:
    return {
        "trace_id": trace_id, "thread_id": thread_id, "ts": ts or _now(),
        "kind": "ask", "org_id": 65, "role": "PRODUCT", "user": user,
        "question": question, "rejected_by": rejected, "attempts": 1,
        "rows_returned": 0, "elapsed_ms": 10, "cost_cny": 0.0,
        "step_count": 1, "multi_step": False, "source": "",
    }


def _write(path: Path, recs: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in recs) + "\n",
                    encoding="utf-8")


@pytest.fixture
def hcfg(cfg, monkeypatch):
    """四个角色各一个账号 —— **两个系统管理员是下限，不是凑数**。

    approvals.decide 的「发起人不得自批」是审批链路唯一一道门；只有一个管理员
    时他自己触发 R-11 的单子永远批不了，那一档对他就是死的。见下面那条用例。
    """
    monkeypatch.setenv(auth.SESSION_SECRET_ENV, SECRET)
    cfg.raw["auth"] = {
        "enabled": True, "required": False,
        "accounts": [
            {"username": "admin1", "roles": ["SYS_ADMIN"],
             "password_hash": auth.hash_password("pw")},
            {"username": "admin2", "roles": ["SYS_ADMIN"],
             "password_hash": auth.hash_password("pw")},
            {"username": "sre1", "roles": ["SRE"],
             "password_hash": auth.hash_password("pw")},
            {"username": "amy", "roles": ["PRODUCT"],
             "password_hash": auth.hash_password("pw")},
        ],
    }
    cfg.raw["observability"] = {**cfg.raw["observability"], "stale_run_after_s": 900}
    return cfg


@pytest.fixture
def hclient(hcfg, monkeypatch):
    monkeypatch.setattr(server, "load", lambda _p: hcfg)
    return TestClient(server.create_app("ignored.yaml"))


def _login(client: TestClient, who: str) -> None:
    r = client.post("/api/auth/login", json={"username": who, "password": "pw"})
    assert r.status_code == 200, r.text


# ===========================================================================
# 能力位：谁能处置什么
# ===========================================================================

def test_handling_capabilities_are_split_by_what_they_judge():
    """三种处置权分属两个角色，**判据是"判的是什么"，不是"官多大"**。

    · APPROVE     审批与复核 —— 判的是这次提问（该不该跑 / 数字算不算数）
    · OPS_RESOLVE 故障处置   —— 判的是系统（库通了没有）

    合成一位就等于说运维=审批：能重启库的人不该因此获得放行一次全表扫描的
    权力，反过来能担成本的人也未必看得懂连接池。
    """
    everyone = identity.caps_of(["PRODUCT"])
    assert identity.caps_of(["SRE"]) - everyone == {identity.OPS_RESOLVE}
    assert identity.APPROVE not in identity.caps_of(["SRE"])
    assert identity.caps_of(["SYS_ADMIN"]) >= {identity.APPROVE, identity.OPS_RESOLVE}
    # 可见面仍然完全相同 —— 多出来的位只决定"能不能把卡住的任务往前推"
    read_only = {c for c in everyone if not c.startswith(("sources.", "eval."))}
    assert read_only <= identity.caps_of(["SRE"])


# ===========================================================================
# 等待审批：批准之后那条任务去哪了
# ===========================================================================

def test_approved_ticket_does_not_fall_into_blocked(hclient, hcfg):
    """**批准的那一刻任务不能掉进「已拦截」。**

    这是闭环断得最隐蔽的一格：折算此前只看"有没有未决审批单"，于是批准之后
    单子不再未决，任务立刻显示成终结态 —— 发起人刚被通知批下来了，回到界面
    看到的却是"这条触碰的是安全边界，改写法也过不去"，而那张票还在审批存储里
    躺着没人用。
    """
    _write(hcfg.audit_log, [_rec("aaaaaaaaaaa1", "111111111111",
                                 user="amy", rejected="R-11")])
    approvals.request(hcfg, trace_id="aaaaaaaaaaa1", user="amy", roles=["PRODUCT"],
                      kind="ask", question="库里有多少文档", sql="SELECT 1",
                      match_text="库里有多少文档", est_rows=999_999,
                      threshold=100_000, source="")

    _login(hclient, "amy")
    before = hclient.get("/api/tasks").json()["items"][0]
    assert before["status"] == audit.WAITING_APPROVAL
    assert before["approval_status"] == "REQUESTED"

    # 没有 APPROVE 的人点不动
    assert hclient.post("/api/approvals/aaaaaaaaaaa1/decide",
                        json={"approved": True, "note": ""}).status_code == 403

    admin = TestClient(server.create_app("ignored.yaml"))
    _login(admin, "admin1")
    assert admin.post("/api/approvals/aaaaaaaaaaa1/decide",
                      json={"approved": True, "note": "放行一次"}).status_code == 200

    after = hclient.get("/api/tasks").json()["items"][0]
    assert after["status"] == audit.WAITING_APPROVAL, "批准后掉档了"
    assert after["approval_status"] == "APPROVED"
    # 状态码没变，但**等的人换了** —— 这句话是这一格存在的意义
    assert "凭票重跑" in after["next_actor"]


def test_one_admin_cannot_close_the_loop_alone(hclient, hcfg):
    """只有一个系统管理员时，他自己的审批单**永远批不了**。

    这条用例钉的不是代码，是部署：public.yaml 里必须有两个 SYS_ADMIN。
    一个人的审批等于没有审批 —— 删掉其中一个之前先读这里。
    """
    _write(hcfg.audit_log, [_rec("aaaaaaaaaaa2", "111111111112",
                                 user="admin1", rejected="R-11")])
    approvals.request(hcfg, trace_id="aaaaaaaaaaa2", user="admin1",
                      roles=["SYS_ADMIN"], kind="ask", question="q", sql="SELECT 1",
                      match_text="q", est_rows=999_999, threshold=100_000, source="")

    _login(hclient, "admin1")
    mine = hclient.post("/api/approvals/aaaaaaaaaaa2/decide",
                        json={"approved": True, "note": "自己批自己"})
    assert mine.status_code == 403
    assert "另一位" in mine.json()["detail"]

    other = TestClient(server.create_app("ignored.yaml"))
    _login(other, "admin2")
    assert other.post("/api/approvals/aaaaaaaaaaa2/decide",
                      json={"approved": True, "note": "同意"}).status_code == 200


# ===========================================================================
# 等待复核
# ===========================================================================

def test_review_queue_is_visible_to_owner_but_decidable_only_by_admin(hclient, hcfg):
    """发起人看得到自己那条被判成什么，但判定权在系统管理员。

    发起人必须看得到 —— 否则他不知道手上那个数字还能不能用。
    """
    blind = _rec("bbbbbbbbbbb1", "111111111113", user="amy", rejected=None)
    blind["recall_blind"] = True          # 盲选召回 = 值得复核的痕迹之一
    _write(hcfg.audit_log, [blind])

    _login(hclient, "amy")
    queue = hclient.get("/api/reviews").json()
    assert queue["pending"] >= 1
    assert queue["can_review"] is False
    assert hclient.post("/api/reviews/bbbbbbbbbbb1/decide",
                        json={"accepted": True, "note": ""}).status_code == 403

    admin = TestClient(server.create_app("ignored.yaml"))
    _login(admin, "admin1")
    assert admin.get("/api/reviews").json()["can_review"] is True
    assert admin.post("/api/reviews/bbbbbbbbbbb1/decide",
                      json={"accepted": False, "note": "盲选召回，不采信"}).status_code == 200

    row = [t for t in hclient.get("/api/tasks").json()["items"]
           if t["thread_id"] == "111111111113"][0]
    assert row["status"] == audit.REVIEW_RETURNED


# ===========================================================================
# 等待运维
# ===========================================================================

def test_exec_failure_can_finally_leave_the_queue(hclient, hcfg):
    """执行期故障这一档**此前只进不出**：审计会把它判成 needs_operator、
    界面写着「等运维」，而系统里既没有运维角色也没有任何写端点。"""
    _write(hcfg.audit_log, [_rec("ccccccccccc1", "111111111114",
                                 user="amy", rejected="EXEC")])

    _login(hclient, "amy")
    assert hclient.get("/api/tasks").json()["items"][0]["status"] == audit.NEEDS_OPERATOR
    assert hclient.post("/api/ops/ccccccccccc1/resolve",
                        json={"status": "RESOLVED", "note": ""}).status_code == 403

    sre = TestClient(server.create_app("ignored.yaml"))
    _login(sre, "sre1")
    queue = sre.get("/api/ops").json()
    assert queue["can_resolve"] is True and queue["pending"] >= 1
    # 结论取值是枚举，不是自由文本 —— 非法值当场 400，别落进存储
    assert sre.post("/api/ops/ccccccccccc1/resolve",
                    json={"status": "MAYBE", "note": ""}).status_code == 400
    assert sre.post("/api/ops/ccccccccccc1/resolve",
                    json={"status": "RESOLVED", "note": "库已恢复"}).status_code == 200

    row = hclient.get("/api/tasks").json()["items"][0]
    assert row["status"] == audit.REJECTED, "处置过还挂在队列上"
    assert row["ops_status"] == "RESOLVED"


def test_a_zombie_thread_can_actually_be_disposed(hclient, hcfg):
    """队列列得出的，处置接口就必须收 —— **第二次线上实测抓出来的回归**。

    2026-09-12：修好队列漏列之后，点下去仍然 404。准入判据当时写死的是
    `rejected_by == "EXEC"`，而僵尸线程（进程中途退出、只落了发起记录）的
    rejected_by 是 None —— 它靠陈旧改判才进的这一档，那条硬编码认不出它。

    同一个「列得出来、动不了」换了个位置又犯一次，所以队列与准入现在共用
    server._pending_ops，判据只此一份。
    """
    _write(hcfg.audit_log, [{
        "trace_id": "fff666666666", "thread_id": "111111111124",
        "ts": _now(-7200), "kind": "ask", "phase": audit.PHASE_STARTED,
        "user": "amy", "question": "跑一半就没了", "source": "",
        "rejected_by": None, "org_id": 65,
    }])
    _login(hclient, "sre1")
    assert [i["trace_id"] for i in hclient.get("/api/ops").json()["items"]] \
        == ["fff666666666"]

    r = hclient.post("/api/ops/fff666666666/resolve",
                     json={"status": "WONTFIX", "note": "无现场可恢复"})
    assert r.status_code == 200, r.text
    assert hclient.get("/api/ops").json()["pending"] == 0


def test_ops_resolve_refuses_records_that_are_not_exec_failures(hclient, hcfg):
    """不是执行期故障的记录不能被标成"已处置" —— 与"不存在"同一响应，
    这个端点不是用来试探某条记录存不存在的。"""
    _write(hcfg.audit_log, [_rec("ccccccccccc2", "111111111115",
                                 user="amy", rejected="R-03")])
    sre = hclient
    _login(sre, "sre1")
    r = sre.post("/api/ops/ccccccccccc2/resolve",
                 json={"status": "RESOLVED", "note": "x"})
    assert r.status_code == 404


def test_ops_store_replays_state_from_events(cfg):
    """处置流水与审批、复核同构：只存事件，状态靠回放推导。

    后写的覆盖先写的 —— 这样任何一次写入失败都只是少一个事件，
    不会留下一条半更新的记录。
    """
    assert ops.decided(cfg) == {}
    ops.resolve(cfg, "ddddddddddd1", operator="sre1", status=ops.RESOLVED,
                note="重启了连接池", owner="amy")
    assert ops.decided(cfg) == {"ddddddddddd1": ops.RESOLVED}

    # 改主意：后写的那条说了算
    ops.resolve(cfg, "ddddddddddd1", operator="sre1", status=ops.WONTFIX,
                note="其实源已下线", owner="amy")
    assert ops.decided(cfg) == {"ddddddddddd1": ops.WONTFIX}

    state = ops.state(cfg)["ddddddddddd1"]
    assert state["operator"] == "sre1" and state["note"] == "其实源已下线"
    # 与审计日志同目录、各自一个文件：三条队列都以 trace_id 为主键，
    # 挤在一起会互相覆盖
    assert ops.store(cfg) != cfg.audit_log
    assert ops.store(cfg).exists()

    with pytest.raises(ValueError):
        ops.resolve(cfg, "ddddddddddd1", operator="sre1", status="BOGUS")


def test_ops_listing_puts_pending_first(cfg):
    """队列排序：待处置在前。已决的沉到后面，但**不消失** ——
    "没人处理"和"处理过了，结论是没救"必须分得开，否则运维每天都要
    重新看一遍同一批注定处理不了的任务。"""
    ops.resolve(cfg, "eeeeeeeeeee1", operator="sre1", status=ops.RESOLVED)
    rows = ops.listing(cfg, [{"trace_id": "eeeeeeeeeee2", "thread_id": "t2"}])
    assert [r["ops_status"] for r in rows] == ["", ops.RESOLVED]


# ===========================================================================
# 等待补充 / 可续跑：clarify 节点的出口
# ===========================================================================

def test_clarification_is_required_and_owned(hclient, hcfg):
    """补充重跑的三道门：要有补充、要是本人、任务要真的在等。

    **空补充不重跑**是有意的：没有活检查点意味着这条线程已经正常收尾，
    不带任何新信息再跑一遍，拿到的必然还是同一个"信息不足"，只是白花一次配额。
    """
    _write(hcfg.audit_log, [_rec("ddddddddddd2", "111111111116",
                                 user="amy", rejected="NO_SQL", question="那前三名呢")])
    _login(hclient, "amy")
    assert hclient.get("/api/tasks").json()["items"][0]["status"] == audit.WAITING_INPUT

    # 空补充 → 与"不存在"同一响应
    assert hclient.post("/api/resume", json={"thread_id": "111111111116",
                                             "clarification": ""}).status_code == 404

    other = TestClient(server.create_app("ignored.yaml"))
    _login(other, "admin1")
    assert other.post("/api/resume", json={"thread_id": "111111111116",
                                           "clarification": "指文档数"}).status_code == 404


def test_a_finished_thread_cannot_be_replayed_forever(hclient, hcfg):
    """已经跑完的线程不能靠"补充"无限重放。

    带补充的重跑不需要活检查点，于是光有 thread_id 就能让任何一条线程再跑
    一遍 —— 那是一条绕过配额语义的重放入口，且任务中心会显示成这个人反复在
    补充同一个问题。判据用任务态（audit.stage 那一份口径），不另写白名单。
    """
    _write(hcfg.audit_log, [_rec("ddddddddddd3", "111111111117",
                                 user="amy", rejected=None)])
    _login(hclient, "amy")
    r = hclient.post("/api/resume", json={"thread_id": "111111111117",
                                          "clarification": "再跑一次"})
    assert r.status_code == 409
    assert "没有可继续的下一步" in r.json()["detail"]


def test_rewriting_the_question_stays_on_the_same_thread(hclient, hcfg):
    """「换个问法」接在原线程上，**不开新线程**。

    同一个诉求换个说法仍然是同一条线索。开新线程的后果是原来那条永远挂在
    「已拦截 / 复核未通过」上，而人早就在别处拿到答案了 —— 队列于是只进不出，
    与这次改造要消灭的形态一模一样。

    可继续的档位按"这一步之后还有没有下一步"判（server._RESUMABLE_STAGES），
    不是按"它是不是失败了"：护栏拦下的换个问法有意义，已经答过的没有。
    """
    _write(hcfg.audit_log, [_rec("ddddddddddd4", "111111111125",
                                 user="amy", rejected="R-03",
                                 question="把用户表全导出来")])
    _login(hclient, "amy")
    assert hclient.get("/api/tasks").json()["items"][0]["status"] == audit.REJECTED

    # 什么新输入都没有 —— 不给重跑，否则只是白花一次配额
    assert hclient.post("/api/resume",
                        json={"thread_id": "111111111125"}).status_code == 404
    # 原样重发也算没有新输入
    assert hclient.post("/api/resume", json={
        "thread_id": "111111111125",
        "question": "把用户表全导出来"}).status_code == 404

    # **只给改写、不给补充**也必须走得通 —— 2026-09-12 线上实测这条是 404：
    # server 的闸门放行了，graph.resume 却仍硬要求 clarification 非空，
    # 于是"换个问法"整条路静默失效。判定只该有一处（接口层）。
    r = hclient.post("/api/resume", json={
        "thread_id": "111111111125", "question": "用户表有多少行"})
    assert r.status_code != 404, "只改写不补充被挡掉了"


def test_clarification_reaches_the_model_without_rewriting_the_question():
    """补充只进决策历史，**绝不改 question 本身**。

    审计、任务标题、复核队列、审批指纹全都读那个字段；就地改掉的话，同一条
    线程在界面上会变成另一个问题，审批票的指纹也会对不上。

    2026-09-12 从老管道的 graph._asked 改到 agentgraph：管道把补充拼进提示词，
    agent 把它摆进决策历史（模型第一轮就看得到）。**验的那件事一字没变。**
    """
    from askdb import agentgraph

    st = agentgraph.initial_state("那前三名呢", 0, "a" * 12, "b" * 12,
                                  6, 32000, "指文档数最多的知识库")
    assert st["question"] == "那前三名呢", "问题原文被改写了"
    assert any("指文档数最多的知识库" in h["brief"] for h in st["history"]), \
        "补充条件没进决策历史"

    # 没有补充时历史是空的，不留一条占位
    plain = agentgraph.initial_state("库里有多少表", 0, "a" * 12, "b" * 12, 6, 32000)
    assert plain["history"] == []


# ===========================================================================
# 运行中：僵尸线程回收
# ===========================================================================

def test_a_long_running_thread_stops_claiming_to_be_running(hclient, hcfg):
    """只落了发起记录、又过了阈值的线程，不再叫"运行中"。

    审计上"正在跑"与"进程被杀了"分不开，但**时间能分开**：没有哪条查询会跑
    一刻钟还不收尾。不判这一下的后果实测过 —— 2026-09-11 线上 8 条线程停在
    「运行中」，最久的卡了十三个半小时，没有任何机制会再看它们一眼。

    改判成哪一档要看现场在不在检查点里：在就是可续跑，不在就是执行期故障。
    这里没有检查点，所以落到等运维。
    """
    started = {"trace_id": "fff111111111", "thread_id": "111111111118",
               "ts": _now(-7200), "kind": "ask", "phase": audit.PHASE_STARTED,
               "user": "amy", "question": "跑一半就没了", "source": "",
               "rejected_by": None, "org_id": 65}
    _write(hcfg.audit_log, [started])

    _login(hclient, "amy")
    row = hclient.get("/api/tasks").json()["items"][0]
    assert row["status"] == audit.NEEDS_OPERATOR
    assert row["stale"] is True

    # 刚发起的那些一动不动 —— 阈值不能把正在跑的误判成中断
    _write(hcfg.audit_log, [{**started, "ts": _now(-10),
                             "trace_id": "fff222222222",
                             "thread_id": "111111111119"}])
    fresh = hclient.get("/api/tasks").json()["items"][0]
    assert fresh["status"] == audit.RUNNING


def test_the_ops_queue_lists_exactly_what_the_task_center_counts(hclient, hcfg):
    """任务中心说有几条待处置，运维队列就得列得出几条。

    **这条用例是一次线上实测抓出来的回归**（2026-09-12）：陈旧线程的定档原来
    写在 /api/tasks 的端点体里，而 /api/ops 直接按状态筛 —— 于是任务中心显示
    9 条等待运维、运维队列只列得出 1 条，那 8 条僵尸线程在"该去处理它们的
    那一页"上根本看不见。

    折算口径只能有一份（server._settle_stale）。新增任何一个按状态取任务的
    接口都要经过它 —— 各算各的必然漂，而漂的表现就是这两个数字对不上。
    """
    _write(hcfg.audit_log, [
        # 真正的执行期故障
        _rec("ccccccccccc3", "111111111122", user="amy", rejected="EXEC"),
        # 进程被杀留下的陈旧线程：审计先判可续跑，核不过检查点才落到等运维
        {"trace_id": "fff555555555", "thread_id": "111111111123",
         "ts": _now(-7200), "kind": "ask", "phase": audit.PHASE_STARTED,
         "user": "amy", "question": "跑一半就没了", "source": "",
         "rejected_by": None, "org_id": 65},
    ])
    _login(hclient, "sre1")

    counted = hclient.get("/api/tasks?page_size=50").json()["stats"]["needs_operator"]
    listed = hclient.get("/api/ops").json()
    assert counted == 2
    assert listed["pending"] == counted, "任务中心的计数与运维队列对不上"
    assert {i["trace_id"] for i in listed["items"] if not i.get("ops_status")} == {
        "ccccccccccc3", "fff555555555"}


def test_stale_judgement_is_off_when_threshold_is_zero(hcfg):
    """阈值 0 = 关掉这项判定。部署形态不同，"多久算死了"也不同 ——
    本机跑一条复杂多步链路可以拖很久，而 k8s 上 pod 被杀是秒级的事。"""
    started = {"trace_id": "fff333333333", "thread_id": "111111111120",
               "ts": _now(-7200), "kind": "ask", "phase": audit.PHASE_STARTED,
               "user": "amy", "question": "q", "source": "", "rejected_by": None}
    _write(hcfg.audit_log, [started])
    off = audit.tasks(hcfg.audit_log, stale_after_s=0)
    assert off[0]["status"] == audit.RUNNING and off[0]["stale"] is False


def test_a_record_without_timestamp_is_never_called_stale(hcfg):
    """取不出时间戳一律当"刚写的"。

    方向是有意选的：宁可让一条真死掉的线程多挂一会儿，也不能因为 ts 缺失或
    格式怪就把**正在跑的**判成中断 —— 那会给出一个「可续跑」入口，
    点下去续的是一条还在跑的线程。
    """
    for bad_ts in ("", "昨天下午", None):
        rec = {"trace_id": "fff444444444", "thread_id": "111111111121",
               "kind": "ask", "phase": audit.PHASE_STARTED, "user": "amy",
               "question": "q", "source": "", "rejected_by": None}
        if bad_ts is not None:
            rec["ts"] = bad_ts
        _write(hcfg.audit_log, [rec])
        rows = audit.tasks(hcfg.audit_log, stale_after_s=900)
        assert rows[0]["stale"] is False, f"ts={bad_ts!r} 被误判为陈旧"


# ===========================================================================
# 交接出去的长任务：原地接管与窗口对齐（2026-09-13）
# ===========================================================================

def test_task_detail_reports_progress_while_running(hclient, hcfg):
    """交接之后查询页轮询这里 —— 还在跑就报「运行中」，不编一个结果出来。"""
    _write(hcfg.audit_log, [{**_rec("bbbbbbbbbbb1", "222222222222",
                                    user="amy", rejected=None),
                             "phase": audit.PHASE_STARTED}])
    _login(hclient, "amy")
    r = hclient.get("/api/tasks/222222222222")
    assert r.status_code == 200
    body = r.json()
    assert body["running"] and body["status"] == audit.RUNNING
    assert body["next_actor"] == "系统正在执行"
    # 检查点里没有这条线程 —— 报不出进度就不报，别编一个第 0 步
    assert body["progress"] is None


def test_task_detail_hands_back_result_block(hclient, hcfg):
    """跑完了：交接暂存取不到就退回审计结果块。**少几个字段，不是失败。**"""
    _write(hcfg.audit_log, [{**_rec("bbbbbbbbbbb2", "333333333333",
                                    user="amy", rejected=None),
                             "answer": "共 15,669 条", "columns": ["n"],
                             "rows_preview": [[15669]], "rows_returned": 1}])
    _login(hclient, "amy")
    body = hclient.get("/api/tasks/333333333333").json()
    assert not body["running"] and body["status"] == audit.DONE
    assert body["result_block"]["answer"] == "共 15,669 条"


def test_task_detail_is_owner_only(hclient, hcfg):
    """有主的任务只有发起人看得到详情 —— 不存在与看不到同为 404。"""
    _write(hcfg.audit_log, [_rec("bbbbbbbbbbb3", "444444444444",
                                 user="amy", rejected=None)])
    _login(hclient, "sre1")
    assert hclient.get("/api/tasks/444444444444").status_code == 404
    assert hclient.get("/api/tasks/notexistxxxx").status_code == 404


def test_open_approval_thread_survives_the_window(hcfg):
    """**等人动手的线程不能因为滑出窗口就消失。**

    窗口按"最近发生了什么"取，而一张审批单可以躺 4 天 —— 交接出去的任务
    尤其吃这个亏，人本来就不在场，回来得更晚。
    """
    old = _rec("ccccccccccc1", "555555555555", user="amy", rejected="R-11",
               ts=_now(-86400 * 3))
    fresh = [_rec(f"ddddddddddd{i}", f"66666666666{i}", user="amy", rejected=None)
             for i in range(3)]
    _write(hcfg.audit_log, [old, *fresh])

    # 窗口只装得下最近两条：那张躺了三天的审批单被挤出去了
    without = audit.tasks(hcfg, max_threads=2)
    assert "555555555555" not in {t["thread_id"] for t in without}
    # 钉住之后它回到这一页，且状态仍是「等审批」
    pinned = audit.tasks(hcfg, max_threads=2, pin_traces=("ccccccccccc1",),
                         approval_status={"ccccccccccc1": approvals.REQUESTED})
    row = [t for t in pinned if t["thread_id"] == "555555555555"]
    assert row and row[0]["status"] == audit.WAITING_APPROVAL


def test_stale_and_next_actor_have_one_definition(hcfg):
    """陈旧判定与「下一步该谁动手」各只有一份 —— 任务中心与任务详情共用。

    两处各写一遍的表现是：同一条线程在列表里是「等运维」、点进去是「运行中」。
    """
    started = {**_rec("eeeeeeeeeee1", "777777777777", user="amy", rejected=None,
                      ts=_now(-3600)), "phase": audit.PHASE_STARTED}
    assert audit.is_stale_run(started, 900)
    assert not audit.is_stale_run(started, 0)          # 0 = 关掉这项判定
    assert not audit.is_stale_run({**started, "phase": ""}, 900)
    assert audit.next_actor(audit.WAITING_APPROVAL, "APPROVED") == audit.NEXT_ACTOR_APPROVED
    assert audit.next_actor(audit.WAITING_APPROVAL) != audit.NEXT_ACTOR_APPROVED
