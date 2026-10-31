"""Small default tool surface; internal adapters remain for compatibility and tests."""
import os

LEGACY_TOOLS=frozenset({
 'inject_login_cookies_cdp','attach_course_cdp','observe_course_cdp','inspect_course_media_cdp',
 'submit_course_quiz_cdp','confirm_course_quiz_cdp',
 'connect_qq_mail','qq_mail_status','qq_mail_call','configure_qq_mail_notifications','qq_mail_notification_events',
 'begin_learning_flow','observe_learning_flow','acknowledge_flow_credentials','mark_flow_login_attempt',
 'defer_learning_item','learning_flow_progress','plan_learning_browser_recovery',
 'inspect_course_media','confirm_course_quiz',
 'inspect_owned_rate_sources','apply_owned_rate_debug_test'
})

def tool_enabled(name):
    return name not in LEGACY_TOOLS or os.environ.get('STUDYFLOW_ENABLE_LEGACY_TOOLS')=='1'

def visible_tools(tools):
    return [t.__name__ for t in tools if tool_enabled(t.__name__)]
