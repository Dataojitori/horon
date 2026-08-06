import pytest
from conftest import create_concepts

def test_plan_result_exclusive_bypass_fix(horon_db):
    """
    Test that the exclusive limitation of plan->result cannot be bypassed.
    Creating a Plan->Result relation and then adding another variation
    to the same holding concept should be rejected.
    """
    create_concepts(horon_db, ["myplan", "myresult", "A", "B", "rel"])
    horon_db.add("myplan", "tag", "plan")
    horon_db.add("myresult", "tag", "result")

    # 1. Create a relation concept with only one Plan -> Result variant
    horon_db.set("rel", "expression", "myplan → myresult")
    
    # 2. Add a second irrelevant variant to this relation concept
    # This should now be rejected by the plan plugin because 'rel' holds a plan->result variation
    with pytest.raises(ValueError, match="排异反应：概念 .* 试图包含 \\[Plan -> Result\\] 闭环"):
        horon_db.add("rel", "variation", "A → B")

def test_plan_result_exclusive_bypass_by_tag_addition(horon_db):
    create_concepts(horon_db, ["P", "R", "A", "B", "C"])
    
    # C has multiple variations
    horon_db.set("C", "expression", "P → R")
    horon_db.add("C", "variation", "A → B")
    
    # Now add result to R, and plan to P
    horon_db.add("R", "tag", "result")
    
    with pytest.raises(ValueError, match="排异反应"):
        horon_db.add("P", "tag", "plan")

def test_plan_result_exclusive_bypass_by_tag_addition_reverse(horon_db):
    create_concepts(horon_db, ["P", "R", "A", "B", "C"])
    
    # C has multiple variations
    horon_db.set("C", "expression", "P → R")
    horon_db.add("C", "variation", "A → B")
    
    # Now add plan to P, and result to R
    horon_db.add("P", "tag", "plan")
    
    with pytest.raises(ValueError, match="排异反应"):
        horon_db.add("R", "tag", "result")

def test_plan_result_length_bypass_by_tag_addition(horon_db):
    create_concepts(horon_db, ["P", "R", "X", "C"])
    
    # C is a long chain P -> R -> X
    horon_db.set("C", "expression", "P → R → X")
    
    # Now add result to R, and plan to P
    horon_db.add("R", "tag", "result")
    
    with pytest.raises(ValueError, match="排异反应：不允许将明确的 \\[Plan -> Result\\] 验证闭环包裹进更长的序列中"):
        horon_db.add("P", "tag", "plan")

def test_plan_result_length_bypass_by_tag_addition_reverse(horon_db):
    create_concepts(horon_db, ["P", "R", "X", "C"])
    
    # C is a long chain P -> R -> X
    horon_db.set("C", "expression", "P → R → X")
    
    # Now add plan to P, and result to R
    horon_db.add("P", "tag", "plan")
    
    with pytest.raises(ValueError, match="排异反应：不允许将明确的 \\[Plan -> Result\\] 验证闭环包裹进更长的序列中"):
        horon_db.add("R", "tag", "result")
