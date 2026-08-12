"""planner 模块的针对性用例（多步规划的护栏形态）。"""

from askdb import planner

def test_carry_rejects_whole_rows(cfg):
    """§5.3.2 carry_columns_only：只许下传标识列，不许下传整行。

    整行下传既泄露非必要字段（§10.1 多步累积泄露），也会撑爆下一步的 SQL。
    """
    ok, why = planner.carry_within_limit({"kb_ids": [12, 7, 31]}, cfg)
    assert ok, why

    ok, why = planner.carry_within_limit(
        {"rows": [[12, "财务档案", 1842], [7, "历史归档", 604]]}, cfg)
    assert not ok and "整行" in why

    cfg.raw["planner"]["carry_columns_only"] = False
    ok, _ = planner.carry_within_limit({"rows": [[12, "财务档案"]]}, cfg)
    assert ok, "显式关闭后应放行"
