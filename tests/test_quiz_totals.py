from chaoxing_learning_mcp.course_extension import _declared_total_counts


def test_global_total_wins_over_section_subtotals():
    assert _declared_total_counts('题量: 4 满分: 100 一. 多选题（共1题） 二. 单选题（共2题） 三. 判断题（共1题）') == {4}


def test_section_counts_are_not_treated_as_global_total():
    assert _declared_total_counts('一. 多选题（共1题） 二. 单选题（共2题）') == set()


def test_conflicting_global_totals_remain_detectable():
    assert _declared_total_counts('题量:4 总题数:5') == {4, 5}


def test_explicit_global_count_and_unparenthesized_legacy_total():
    assert _declared_total_counts('本测验共4题') == {4}
