from chaoxing_learning_mcp.tool_registry import tool_enabled,LEGACY_TOOLS

def test_default_hides_legacy_but_preserves_current_workflow(monkeypatch):
    monkeypatch.delenv('STUDYFLOW_ENABLE_LEGACY_TOOLS',raising=False)
    for name in LEGACY_TOOLS:assert not tool_enabled(name)
    for name in ('setup_learning_accounts','add_parallel_learning_accounts','submit_reviewed_chapter_quiz',
                 'control_owned_video','attach_exam_browser','prepare_course_quiz','send_learning_stage_notice'):
        assert tool_enabled(name)

def test_explicit_legacy_opt_in(monkeypatch):
    monkeypatch.setenv('STUDYFLOW_ENABLE_LEGACY_TOOLS','1')
    assert all(tool_enabled(name) for name in LEGACY_TOOLS)
