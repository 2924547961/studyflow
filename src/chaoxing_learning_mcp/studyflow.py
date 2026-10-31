"""StudyFlow MCP: one entry point for course tasks and exams."""
from __future__ import annotations

from typing import Any, Literal
from mcp.server.mcpserver import MCPServer
from . import __version__, qq_mail, smtp_notifications
from . import course_tasks, exam_tasks, course_extension, course_cdp, course_direct, learning_flow, homework, runtime_updates, workflow_support
from . import batch_runner, parallel_runner
from .tool_registry import tool_enabled,visible_tools

server = MCPServer(
    name="studyflow",
    title="学程 MCP · StudyFlow",
    version=__version__,
    instructions=(
        "Use route_learning_task to select course tasks or exams. "
        "Course tasks include inventory, chapter quizzes, actual playback and deadlines. "
        "Use an exact platform-confirmed cache first; legacy, rejected or partial-credit entries are not verified hits. "
        "On cache miss analyze complete question/options with current session model. When evidence is insufficient or conflicts, "
        "use authoritative web search and have the model re-evaluate the complete question and options. "
        "Non-cache exam reviews require model_reviewed=true, "
        "certain=true and basis; web-assisted reviews also include HTTPS sources. "
        "Review every included and excluded multiple-choice option and enforce any exact count. "
        "Unverified multi-select reviews require policy_version=2, question_scope, scope_basis and option_checks. "
        "Distinguish course_enumeration from general_definition: generic usefulness never proves lesson-list membership. "
        "Course enumeration requires direct course material, an HTTPS source and locator for each option. "
        "Uncertain membership blocks saves; use get_wrong_questions to inspect complete mistakes, never infer keys from rejected combinations. "
        "Wait randomly 2-5 seconds before saving. Only explicit complete per-question platform grading establishes 100%. "
        "Owned-browser exams read full preview once, review uncached questions, then fill sequentially in local scripts. "
        "After a full matching preview, final submission requires explicit per-exam authorization; "
        "start_owned_exam_finish can wait for visible remaining time below the authorized threshold and submit once. "
        "For course/video/quiz first call browser_status; if absent call start_course_browser(account_id), default headless msedge, then goto_course_page. Reuse the same process-owned generation. control_owned_video(action=play) automatically mutes, applies inspected 1.25 adapter, minimizes and arms local monitoring; do not retry menu/native speed. Courses support an explicitly started MCP-owned Playwright browser, using the user's imported HTTP session in memory. The service supports parallel account workers by default when course lists are supplied: one browser per account, no fixed account/concurrency cap; headless (default, no window) or minimized (explicit choice); ask the user once before login and use configure_course_browser or setup_learning_accounts(browser_mode=...); loads tools without launching a browser; starts automatically only after account/mode setup; STUDYFLOW_AUTO_BROWSER=1 is an explicit opt-in for eager startup; do not maximize or foreground it; collect inputs then use setup_learning_accounts to login the first account; within each account reuse its browser; sequential mode may switch isolated contexts in one browser. Never copy daily browser profiles. Routine queue steps run in local scripts without model APIs; question reviews use the current model only when required, routed by parallel_account_tool. Exams retain controller handoff. Controller supplies DOM observations "
        "and performs permitted UI actions; MCP only validates plans and navigation readback. "
        "Before course execution, ask for learning accounts/passwords and offer one optional shared sender via configure_smtp_sender and a separate optional notify_email for each learning account. Never route notices to another account recipient. Skip mail when omitted; never request QQ login password. Import each mapping via import_accounts. Execute accounts in independent parallel workers or explicitly selected sequential mode, and verify each platform task before declaring completion. SMTP reminders attach only verified QR visuals, deduplicate, and refresh only the expired platform QR through visible UI; never complete face verification for the user. Credentials remain in process memory."
    ),
)

@server.tool()
def route_learning_task(task_type: Literal['course', 'quiz', 'video', 'deadlines', 'exam', 'homework', 'inventory'],
                        transport: Literal['browser', 'cdp'] = 'browser') -> dict[str, Any]:
    """Unified read-only entry point. Choose the mode from user intent and observed task type.

    Returns tools and dependencies, not a claim that the session is connected.
    Does not log in, start playback, save answers, or submit anything.
    """
    routes = {
        'inventory': (['list_accounts', 'list_courses', 'inspect_course'], ['explicitly selected own-account HTTP inventory; no browser control, playback or answer submission']),
        'homework': (['attach_course_browser', 'inspect_homework_browser'],
                     ['existing single Edge tab', 'fresh homework-list URL and DOM',
                      'nonempty homework requires a verified homework-specific adapter; no chapter/exam submission fallback']),
        'course': (['list_courses', 'inspect_course'], ['logged-in account_id']),
        'quiz': (['get_chapter_quiz', 'submit_chapter_quiz'],
                 ['logged-in account_id', 'study_url', 'fresh quiz_token and reviewed answers before submission']),
        'video': (['inspect_chapter_video', 'play_chapter_video'],
                  ['logged-in account_id', 'study_url', 'user-requested playback']),
        'deadlines': (['attach_course_browser', 'inspect_homework_browser', 'evaluate_homework_deadlines', 'export_deadlines_ics'],
                      ['fresh observed homework-list DOM for each selected course',
                       'enumerate every list page/filter before claiming complete coverage',
                       'host delivers due_soon/overdue alerts; ICS export alone does not activate monitoring']),
        'exam': (['attach_exam_session', 'read_current_exam_question',
                  'review_current_exam_answer', 'run_exam_question_loop',
                  'exam_loop_progress', 'audit_exam_preview'],
                 ['explicit local CDP endpoint', 'one existing exam tab',
                  'run_id and fresh question_token; no final submission']),
    }
    if task_type in ('course', 'quiz', 'video'):
        routes[task_type] = (
            {'course': ['begin_learning_flow', 'observe_learning_flow', 'mark_flow_login_attempt', 'learning_flow_progress', 'defer_learning_item',
                        'inject_login_cookies_cdp', 'attach_course_cdp', 'observe_course_cdp'],
             'quiz': ['observe_course_browser', 'prepare_course_quiz', 'release_course_quiz', 'confirm_course_quiz',
                      'observe_course_cdp', 'submit_course_quiz_cdp', 'confirm_course_quiz_cdp'],
             'video': ['attach_course_browser', 'inspect_course_media',
                       'attach_course_cdp', 'inspect_course_media_cdp']}[task_type],
            ['one existing Edge tab via browser controller; no new login or browser',
             'fresh controller DOM/media observations; normal UI actions only; login uses user-provided credentials in the SAME browser UI',
             'CDP direct alternative: host-launched persistent browser (chrome --remote-debugging-port=<port> --user-data-dir=<isolated>) plus inject_login_cookies_cdp bridges the logged-in HTTP session; MCP then reads DOM and verifies media itself',
             'single-process direct: start_course_browser launches one owned browser inside the MCP process, seeds HTTP session cookies and reads/acts in the same process (no controller round-trips)',
             'stop on platform multi-terminal warnings; skip already graded quizzes'])
    if transport not in ('browser', 'cdp'):
        raise ValueError('unsupported transport')
    if task_type == 'exam' and transport == 'browser':
        routes['exam'] = (
            ['attach_exam_browser', 'prepare_exam_browser_step',
             'wait_exam_browser_save', 'confirm_exam_browser_draft', 'refresh_exam_browser',
             'exam_browser_progress', 'audit_exam_browser_preview'],
            ['existing tab in an available browser controller',
             'fresh URL and DOM observations from supported read-only browser APIs',
             'controller executes normal allowed UI actions and obtains independent readback',
             'no CDP endpoint required; MCP itself does not drive the extension'])
    if task_type not in routes:
        raise ValueError('unsupported task_type')
    workflow, requirements = routes[task_type]
    if task_type in ('course', 'quiz', 'video'):
        direct = {
            'course': ['setup_learning_accounts', 'browser_status', 'start_course_browser', 'goto_course_page', 'find_chapter_links',
                       'inspect_owned_page', 'open_course_chapter', 'browser_status'],
            'quiz': ['browser_status', 'start_course_browser', 'goto_course_page', 'open_chapter_quiz', 'observe_chapter_quiz', 'submit_reviewed_chapter_quiz', 'read_chapter_quiz_grading'],
            'video': ['browser_status', 'start_course_browser', 'goto_course_page', 'select_course_card', 'control_owned_video', 'monitor_owned_video'],
        }
        workflow = list(dict.fromkeys(direct[task_type]))
        requirements = ['explicitly authorized requests login + dedicated Playwright browser; one MCP process owns the page across calls',
                        'tool discovery does not launch a browser; setup starts one automatically, default headless and channel=msedge; reuse the process-owned instance, stop on identity/account gates',
                        'control_owned_video(play) initializes muted playback, applies the inspected 1.25 adapter, minimizes and arms local event monitoring; never cycle through absent menu/native fallback',
                        'an existing headless instance is reused without silently replacing it; browser_status gives actual state']
    return {'server': 'studyflow', 'mode': 'exam' if task_type == 'exam' else 'course', 'task_type': task_type,
            'workflow': workflow, 'requirements': requirements,
            'setup_input': {'request_learning_credentials':not bool(course_tasks._accounts),
                            'required_per_account':['account_id','username','password'],
                            'optional_per_account':['notify_email'],
                            'optional_shared_smtp':['sender_email','smtp_auth_code'],
                            'shared_sender_configured':bool(smtp_notifications._shared_sender),
                            'qq_login_password_required':False,
                            'browser_mode_choices':['headless','minimized'],
                            'default_browser_mode':'headless',
                            'default_execution_mode':'parallel',
                            'fixed_account_limit':None,
                            'optional_per_account_courses':'courses list enables automatic script execution',
                            'skip_email_allowed':True,
                            'reuse_existing_memory_credentials':True},
            'platform_write': False, 'auto_final_submit': False,
            'transport': transport if task_type == 'exam' else 'single_process_direct' if task_type in ('course', 'quiz', 'video') else 'existing_browser_controller' if task_type in ('homework', 'deadlines') else 'account_http',
            'answer_rules': ['exact platform-confirmed cache first; reject wrong combinations and legacy hits',
                             'on cache miss current-session model analyzes complete question/options and course material',
                             'if uncertain or conflicting, search authoritative sources then re-review with model',
                             'multi-select: exact count and reasons for every included/excluded option',
                             'never reuse rejected combinations or guess unresolved answers']}



@server.tool()
def capabilities() -> dict[str, Any]:
    """Describe supported operations and explicit mutation boundaries."""
    return {
        "supported": [
            "exam parsing and reviewed single-question plans",
            "memory-only batch account import",
            "batch login",
            "course inventory",
            "course/chapter page inspection",
            "incomplete task hints",
            "deadline extraction",
            "ICS reminder export",
            "returning user-openable course URLs",
            "structured decoded chapter-quiz extraction",
            "local exact-question verified-answer cache",
            "explicit reviewed-answer submission",
            "bounded randomized pre-submit delay",
            "existing-tab course DOM and actual media observation validation",
        ],
        "exam_transport": {
            "mode": "one question at a time", "live_verified": False,
            "default": "browser_controller_handoff",
            "browser": {"requires": "available browser controller and fresh DOM observations",
                        "mcp_executes_browser_actions": False,
                        "verification": "controller-supplied independent navigation readback"},
            "cdp": {"requires": "explicit local CDP endpoint and existing exam tab",
                    "automatic_navigation": True},
            "final_submission": False, "signing": "existing page JavaScript"},
        "course_transport": {"default": "single_process_direct", "browser_launches": 0,
                             "legacy_independent_browser_disabled": True, "single_tab_binding": True,
                             "mcp_executes_browser_actions": True, "live_verified": False,
                             "tools": visible_tools(course_extension.TOOLS),
                             "cdp_direct": {"tools": visible_tools(course_cdp.TOOLS),"enabled":tool_enabled("attach_course_cdp"),
                                            "requires": "host-launched persistent browser with an explicit local CDP endpoint (e.g. chrome --remote-debugging-port=9333 --user-data-dir=<isolated dir>)",
                                            "login_bridge": "inject_login_cookies_cdp seeds the logged-in HTTP session cookies; no browser login UI",
                                            "mcp_reads_dom_and_media_directly": True,
                                            "submission": "submit_course_quiz_cdp executes exactly one released reviewed plan; grading readback included",
                                            "browser_launches": 0, "profile_copies": 0},
        "single_process_direct": {"tools": visible_tools(course_direct.TOOLS),
                                  "model": "early architecture: MCP process launches its own Playwright browser and does login reuse, iframe locate, select, submit and grading readback in one process",
                                  "requires": "browser starts on demand after mode/account setup; headless by default",
                                  "login": "HTTP session cookies only; no browser login UI",
                                  "timeouts": {"worker_s": 45, "launch_s": 30, "navigate_s": 25, "iframe_polls": 12,
                                               "option_click_s": 10, "submit_s": 15, "grading_s": 10},
                                  "retries": {"launch": 2, "navigate": 3, "option_click": 2,
                                              "submit": "one attempt only", "grading_read": 3},
                                  "logging": "browser_run_log returns/writes timestamped per-stage entries"}},
        "account_execution": {"default":"parallel", "fixed_account_limit":None,
                              "browser_per_account":True, "routine_model_api_calls":0,
                              "parallel_tools":[t.__name__ for t in parallel_runner.TOOLS],
                              "sequential_tools":[t.__name__ for t in batch_runner.TOOLS],
                              "completion":"independent platform directory per account/course",
                              "review":"explicit model review required; unknown submissions never replayed"},
        "version": __version__,
        "tool_surface":{"profile":"legacy_compatibility" if tool_enabled("attach_course_cdp") else "direct_core","legacy_opt_in":"STUDYFLOW_ENABLE_LEGACY_TOOLS=1"},
        "course_inventory": {"adapter_revision": 2, "transport": "account_http",
                             "tools": ["list_courses", "inspect_course"],
                             "chapter_source": "observed homepage chapter data-url and current course fields",
                             "menus_are_tasks": False, "unknown_is_complete": False,
                             "verification_9010_latches_account": True,
                             "automatic_code_refresh": "before inspect_course; no transport restart",
                             "playback_or_submission": False},
        "homework_transport": {"tools": [t.__name__ for t in homework.TOOLS],
                               "inventory_only": True, "automatic_submission_supported": False,
                               "nonempty_inventory": "explicit observed links, pending/submitted status and deadline labels; unsupported layouts unknown",
                               "deadline_tools": [t.__name__ for t in workflow_support.TOOLS],
                               "active_reminder_service": False},
        "learning_flow": {"tools": visible_tools(learning_flow.TOOLS),"enabled":tool_enabled("begin_learning_flow"),
                          "login": "existing-browser UI plan with one-attempt readback; no independent HTTP session",
                          "queue": "ordered exact course IDs/names, explicit deferred records, observed progress only",
                          "requires_host_browser_controller": True, "autonomous_browser_driver_in_server": False,
                          "live_end_to_end_verified": False,
                          "user_gates": ["action-time agreement acceptance", "CAPTCHA", "face verification pauses and requires user notification", "account-level warnings"]},
        "not_registered": [
            "default, random or guessed answers",
            "submission without a fresh matching observation and reviewed one-use plan",
            "fabricated video progress or study time",
        ],
        "answer_policy": {
            "review_policy_version": 2,
            "mistake_tool": "get_wrong_questions",
            "course_enumeration_rule": "Direct course evidence for every included/excluded option; general truth and third-party keys alone are insufficient",
            "multi_select_fields": ["policy_version=2", "question_scope", "scope_basis", "option_checks for all current options"],
            "priority": ["exact platform-confirmed cache", "current-session model analysis on cache miss", "authoritative web search only if evidence insufficient or conflicting", "current-model re-review of sourced evidence"],
            "conflict_rule": "authoritative factual evidence outranks unverified third-party answer banks",
            "broad_multi_select_rule": "if the stem gives no exact count and option boundaries overlap, require corroboration before submission",
            "all_multi_select_rule": "review every selected and excluded option, including exact-count questions",
            "partial_credit_rule": "partial credit only rejects the submitted combination; it never establishes the missing option or full answer",
            "source_rule": "course-specific names, scenes and quotations require course evidence; external answer banks are secondary",
            "cache_rule": "exact question/options fingerprint and platform_confirmed required; rejected combinations blocked even if marked confirmed",
            "exam_review_fields": ["valid platform-confirmed exact cache OR model_reviewed=true", "certain=true", "basis", "sources when web-assisted"],
            "pre_save_delay_seconds": [2, 5],
            "grading_tool": "inspect_exam_grading_html",
            "web_search_trigger": "model uncertainty, missing course-specific evidence, contradictory sources, or ambiguous overlapping options",
            "uncertain_action": "do not submit",
            "success_claim": "report 100% only after platform per-question verification",
        },
    }



@server.tool()
def refresh_direct_implementation(discard_unsubmitted_plan: bool = False) -> dict[str, Any]:
    """Explicitly reload direct course code while preserving its idle browser and thread.

    Never reload while a submission outcome is unknown. An unsubmitted review
    plan may be explicitly discarded and must then be prepared again.
    """
    import hashlib
    import importlib
    import os
    from pathlib import Path
    with course_direct._operation_lock:
        if course_direct._worker_failed:
            raise ValueError('Worker outcome unresolved; do not hot reload')
        if course_direct._state.get('submit_attempted'):
            # A submit whose confirmation failed leaves the account pinned forever.
            # When the platform itself reports the quiz graded, the write already
            # landed, so release the guard instead of blocking every recovery path.
            if not course_direct._graded_platform_observation():
                raise ValueError('Worker/submission outcome unresolved; do not hot reload')
            course_direct._state['submit_attempted'] = False
            course_extension._pending = None
        if course_extension._pending and not discard_unsubmitted_plan:
            raise ValueError('Discard the unsubmitted plan explicitly before reloading')
        root = Path(course_direct.__file__).parent
        for name in ('course_direct', 'course_extension'):
            compile((root / (name + '.py')).read_bytes(), str(root / (name + '.py')), 'exec')
        modules = (course_extension, course_direct)
        snapshots = [dict(m.__dict__) for m in modules]
        direct_keys = ('_state','_LOG','_jobs','_worker','_lock','_operation_lock',
                       '_worker_lock','_worker_failed','_reported_quizzes')
        extension_keys = ('_binding','_observations','_pending','_warning','_face_gate')
        preserved = ({k:getattr(course_extension,k) for k in extension_keys},
                     {k:getattr(course_direct,k) for k in direct_keys})
        old_names = [t.__name__ for m in modules for t in m.TOOLS]
        try:
            for module, values in zip(modules, preserved):
                importlib.reload(module)
                module.__dict__.update(values)
            from mcp.server.mcpserver.tools import Tool
            for module in modules:
                for tool in module.TOOLS:
                    if tool_enabled(tool.__name__):Tool.from_function(tool)
        except Exception:
            for module, values in zip(modules, snapshots):
                module.__dict__.clear()
                module.__dict__.update(values)
            raise
        if discard_unsubmitted_plan:
            course_extension._pending = None
            course_extension._observations.clear()
        registered = {tool.name for tool in server._tool_manager.list_tools()}
        for name in old_names:
            if tool_enabled(name) and name in registered:server.remove_tool(name)
        for module in modules:
            for tool in module.TOOLS:
                if tool_enabled(tool.__name__):server.add_tool(tool)
        fingerprint = hashlib.sha256(b''.join(
            (root/(n+'.py')).read_bytes() for n in ('course_direct','course_extension'))).hexdigest()
        return {'updated':True, 'server_pid':os.getpid(), 'source_fingerprint':fingerprint,
                'browser':course_direct.browser_status(), 'platform_write':False,
                'unsubmitted_plan_discarded':discard_unsubmitted_plan}


from . import runtime_lifecycle

for tool in (runtime_updates.implementation_status, runtime_updates.refresh_inventory_implementation, *course_tasks.TOOLS, *exam_tasks.TOOLS, *course_extension.TOOLS, *course_cdp.TOOLS, *course_direct.TOOLS, *batch_runner.TOOLS, *parallel_runner.TOOLS, *learning_flow.TOOLS, *homework.TOOLS, *workflow_support.TOOLS, *qq_mail.TOOLS, *smtp_notifications.TOOLS):
    if tool_enabled(tool.__name__):server.tool()(tool)


def main() -> None:
    import os
    import threading
    def bootstrap():
        try:
            course_direct.start_course_browser()
        except Exception as exc:
            course_direct._state['auto_start_error']=type(exc).__name__
    # Tool discovery/reloads must not create browsers before mode is selected.
    # setup_learning_accounts/login_accounts start it automatically on demand.
    if os.environ.get('STUDYFLOW_AUTO_BROWSER','0')=='1':
        threading.Thread(target=bootstrap,name='studyflow-browser-bootstrap',daemon=True).start()
    runtime_lifecycle.record('server_started')
    try:
        server.run()
        runtime_lifecycle.record('transport_returned_or_stdin_closed')
    except BaseException as exc:
        runtime_lifecycle.record('server_exception',type(exc).__name__)
        raise
    finally:
        runtime_lifecycle.record('server_shutdown')
        try: course_direct.stop_course_browser()
        except Exception: pass


