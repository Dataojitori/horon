"""Tag 系统回归测试。

覆盖范围：
  - 词表种子（plan / result 随 schema 初始化注册）
  - add <concept> tag <tag>：盖章、幂等、拒绝未注册词
  - delete <concept> tag <tag>：揭章、拒绝揭不存在的章
  - read_concept 返回 tags
  - search_concepts 的 tag 过滤（goal 选单查询）
  - 外键兜底：绕过应用层直插未注册 tag 被数据库拒绝
  - audit_plans 两条 lint（库层逻辑；投放渠道未定，无 CLI 入口）
"""
import sqlite3

import pytest


def test_tag_vocabulary_is_seeded(horon_db):
    registered = {
        row["name"] for row in
        horon_db.conn.execute("SELECT name FROM tags")
    }
    assert {"plan", "result"} <= registered


def test_add_tag_and_read_it_back(horon_db):
    horon_db.create_concept("部署新版本")
    result = horon_db.add("部署新版本", "tag", "plan")
    assert "Tagged" in result.message

    read = horon_db.read_concept("部署新版本")
    assert read.tags == ["plan"]


def test_add_tag_is_idempotent(horon_db):
    horon_db.create_concept("部署新版本")
    horon_db.add("部署新版本", "tag", "plan")
    repeated = horon_db.add("部署新版本", "tag", "plan")
    assert "already has tag" in repeated.message
    assert horon_db.read_concept("部署新版本").tags == ["plan"]


def test_add_unregistered_tag_is_rejected_with_vocabulary_hint(horon_db):
    horon_db.create_concept("部署新版本")
    with pytest.raises(ValueError) as excinfo:
        horon_db.add("部署新版本", "tag", "urgent")
    message = str(excinfo.value)
    assert "not registered" in message
    # 报错里列出已知词表，帮调用者当场纠正
    assert "plan" in message and "result" in message


def test_concept_can_carry_multiple_tags(horon_db):
    horon_db.create_concept("部署新版本")
    horon_db.add("部署新版本", "tag", "plan")
    horon_db.add("部署新版本", "tag", "result")
    # 按字母序返回
    assert horon_db.read_concept("部署新版本").tags == ["plan", "result"]


@pytest.mark.parametrize("operator", ["&", "|"])
def test_plan_tag_rejects_existing_non_chain_expression(horon_db, operator):
    horon_db.create_concept("步骤甲")
    horon_db.create_concept("步骤乙")
    horon_db.create_concept("无序组合")
    horon_db.add("无序组合", "variation", f"步骤甲 {operator} 步骤乙")

    with pytest.raises(ValueError) as excinfo:
        horon_db.add("无序组合", "tag", "plan")

    assert "plans can only contain CHAIN expressions" in str(excinfo.value)
    assert horon_db.read_concept("无序组合").tags == []


def test_delete_tag(horon_db):
    horon_db.create_concept("部署新版本")
    horon_db.add("部署新版本", "tag", "plan")
    result = horon_db.delete("部署新版本", "tag", "plan")
    assert "Removed tag" in result.message
    assert horon_db.read_concept("部署新版本").tags == []


def test_delete_absent_tag_is_rejected(horon_db):
    horon_db.create_concept("部署新版本")
    with pytest.raises(ValueError) as excinfo:
        horon_db.delete("部署新版本", "tag", "plan")
    assert "does not have tag" in str(excinfo.value)

def test_delete_system_tag_is_rejected(horon_db):
    with pytest.raises(ValueError) as excinfo:
        horon_db.delete_tag("plan")
    assert "system-reserved tag and cannot be deleted" in str(excinfo.value)

def test_delete_system_concept_is_rejected(horon_db):
    horon_db.create_concept("plan")
    with pytest.raises(ValueError) as excinfo:
        horon_db.delete("plan")
    assert "system-reserved concept and cannot be deleted" in str(excinfo.value)

def test_rename_system_concept_is_rejected(horon_db):
    horon_db.create_concept("plan")
    with pytest.raises(ValueError) as excinfo:
        horon_db.set("plan", "name", "plan2")
    assert "system-reserved concept" in str(excinfo.value)


def test_search_by_tag_only(horon_db):
    horon_db.create_concept("部署新版本")
    horon_db.create_concept("服务恢复")
    horon_db.create_concept("无关概念")
    horon_db.add("部署新版本", "tag", "plan")
    horon_db.add("服务恢复", "tag", "result")

    goal_candidates = horon_db.search_concepts(tag_expr="result")
    assert [c.name for c in goal_candidates] == ["服务恢复"]


def test_search_intersects_query_and_tag(horon_db):
    horon_db.create_concept("部署新版本")
    horon_db.create_concept("部署回滚")
    horon_db.create_concept("部署文档")   # 文本命中但没有 tag
    horon_db.add("部署新版本", "tag", "plan")
    horon_db.add("部署回滚", "tag", "plan")

    hits = horon_db.search_concepts("回滚", tag_expr="plan")
    assert [c.name for c in hits] == ["部署回滚"]


def test_search_requires_query_or_tag(horon_db):
    with pytest.raises(ValueError):
        horon_db.search_concepts()


def test_foreign_key_rejects_unregistered_tag_bypassing_app_layer(horon_db):
    created = horon_db.create_concept("部署新版本")
    with pytest.raises(sqlite3.IntegrityError):
        horon_db.conn.execute(
            "INSERT INTO concept_tags (concept_id, tag) VALUES (?,?)",
            (created.concept_id, "bogus"),
        )

def test_rename_tag_source_to_same_name(horon_db):
    """验证：当概念是 registered tag 的 source 时，被重命名为其自己（幂等）时不应该报错。"""
    horon_db.create_concept("我的标签")
    horon_db.create_tag("我的标签")
    
    # 不应该抛出 collision 错误
    result = horon_db.set("我的标签", "name", "我的标签")
    assert "Name is already" in result.message


def test_rename_tag_source_cascades_to_tagged_concepts(horon_db):
    horon_db.create_concept("旧标签名")
    horon_db.create_concept("被分类概念")
    horon_db.create_tag("旧标签名")
    horon_db.add("被分类概念", "tag", "旧标签名")

    horon_db.set("旧标签名", "name", "新标签名")

    assert horon_db.read_concept("被分类概念").tags == ["新标签名"]
    assert horon_db.conn.execute(
        "SELECT 1 FROM tags WHERE name = '旧标签名'"
    ).fetchone() is None


# ── audit_plans ──────────────────────────────────────────────


def test_audit_plans_flags_plan_without_expectation(horon_db):
    horon_db.create_concept("计划部署")
    horon_db.add("计划部署", "tag", "plan")

    findings = horon_db.audit_plans()
    assert len(findings) == 1
    assert findings[0].startswith("[malformed]")
    assert "计划部署" in findings[0]


def test_audit_plans_flags_unsettled_expectation(horon_db):
    horon_db.create_concept("计划部署")
    horon_db.add("计划部署", "tag", "plan")
    horon_db.create_concept("服务恢复")
    horon_db.create_concept("部署后服务恢复")
    horon_db.set("部署后服务恢复", "expression", "计划部署 → 服务恢复")

    findings = horon_db.audit_plans()
    assert len(findings) == 1
    assert findings[0].startswith("[open]")
    assert "部署后服务恢复" in findings[0]


def test_audit_plans_is_clean_after_expectation_settled(horon_db):
    horon_db.create_concept("计划部署")
    horon_db.add("计划部署", "tag", "plan")
    horon_db.create_concept("服务恢复")
    horon_db.create_concept("部署后服务恢复")
    horon_db.set("部署后服务恢复", "expression", "计划部署 → 服务恢复")
    # 结账：先确认成员原子概念，再确认期待关系本身
    horon_db.set("计划部署", "status", "confirmed")
    horon_db.set("服务恢复", "status", "confirmed")
    horon_db.set("部署后服务恢复", "status", "confirmed")

    assert horon_db.audit_plans() == []


def test_audit_plans_negated_expectation_is_also_settled(horon_db):
    """否定同样算结账：现实回答了'不会发生'，计划不再欠账。"""
    horon_db.create_concept("计划部署")
    horon_db.add("计划部署", "tag", "plan")
    horon_db.create_concept("服务恢复")
    horon_db.create_concept("部署后服务恢复")
    horon_db.set("部署后服务恢复", "expression", "计划部署 → 服务恢复")
    horon_db.set("部署后服务恢复", "status", "negated")

    assert horon_db.audit_plans() == []
