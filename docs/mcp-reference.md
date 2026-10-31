# 学程 MCP · StudyFlow

> **仅供学习用途，严禁用于刷课。** 请遵守平台规则、课程要求及学术诚信规范；不得用于代学、代考、伪造学习记录、绕过本人验证或批量获取学分。

## 当前直接执行路线（1.9.1，2026-10-21）

课程使用 MCP 进程内 requests 登录会话和 Playwright 独立浏览器。工具发现不启动浏览器；信息收齐后按指定模式启动，默认无头。下文 1.6.x 的浏览器控制器段落为历史接口说明，不代表当前默认路线。

`setup_learning_accounts` 的每条账号可带 `courses` 精确课程名列表及可选 `notify_email`。全部账号有课程列表时，默认 `execution_mode="parallel"` 自动启动；不固定账号/并发数量上限。每账号一个独立 MCP/浏览器进程，账号内严格顺序。`browser_mode="headless"` 无窗口；`"minimized"` 有界面但最小化静音。`execution_mode="sequential"` 保留单浏览器顺序模式。

- `batch_runner.py`：默认本地脚本队列、目录确认、播放和断点状态；不调用模型 API。
- `parallel_runner.py`：独立账号进程及收件隔离；已有当前账号浏览器复用，其余账号各自启动。
- `start_parallel_learning_batches` / `parallel_learning_status`：启动及缓存汇总。
- `parallel_account_tool`：将必要题目观察、模型审阅及单次提交路由到对应账号；审阅后 `resume_learning_batch` 继续。
- `playback_health.py` / `smtp_notifications.py`：进程心跳、持续暂停告警和验证截图邮件，凭据仅内存。

视频的普通检查、恢复和自动推进由脚本完成。未审阅题目、验证码、人脸、账号异常和未知提交保留现场；不跳过冒充完成。资源不足按账号记录失败。状态文件没有密码、cookies、SMTP 授权码或签名 URL；服务重启须重新输入凭据并核验平台进度。

## 历史 1.6.x 接口说明

一个服务，两个业务模块：

- `learning_flow.py`、`course_extension.py`：登录识别、课程队列、章节任务与单会话回读。
- `course_tasks.py`：账号清单、HTTP课程清单辅助、截止事项和错题读取。
- `exam_tasks.py`：考试逐题读取、审阅、暂存、切题和整卷复核。
- `studyflow.py`：统一 MCP 入口与任务分流。

`client.py`、`quiz.py`、`exam.py`、`exam_browser.py`、`exam_extension.py` 和 `assets/` 是底层实现与资源。
当前回归测试位于 tests/。

## 启动

统一注册名为 `studyflow`，唯一模块入口为 `chaoxing_learning_mcp`。将本项目 `src` 加入 `PYTHONPATH` 后运行：

```powershell
python -B -m chaoxing_learning_mcp
```

历史版本 1.6.5。桌面配置已指向同一个入口与源码目录，不需要另建测试服务；运行版本须读取 capabilities 确认。

1.6.5新增 `plan_learning_browser_recovery`：每条流程最多一次的宿主恢复计划，遇待提交/未知结果、
人脸与账号警告时拒绝。恢复路径为空白页DOM验证后在同一Edge会话导航并独立回读，不是MCP内后台驱动。
恢复后若tab改变须显式处理旧绑定，当前实现不自动替换绑定。重复失败停止，不反复开页或登录。

1.6.4补齐门户中实际出现的“学习空间 → 新版课程/课程”控件导航与个人空间课程iframe读取分支。
有同名控件时不猜选，不从截图臆造跳转URL；这些仍是宿主正常UI操作计划，不是后台浏览器驱动。
门户导航出现不等于登录成功，须在后续页面独立确认账号状态。

1.6.3新增明确待完成章节状态解析与首个待完成章节选择，以及 `homework` 路由和
`inspect_homework_browser` 空作业检查。非空作业答题/提交适配尚未实现，不混用章节或考试提交入口。
MCP仍需宿主浏览器控制器执行点击；尚无服务内后台Edge执行器、两分钟通知调度器或系统弹窗。

## 课程：登录到任务队列

`begin_learning_flow(browser_id, tab_id, courses)` 接收按顺序的课程名称、course_id、class_id、用户提供的课程URL。
宿主已持有本轮授权凭据时传 `credentials_available_to_controller=true`；否则流程返回请求凭据，
宿主收到用户输入后调用 `acknowledge_flow_credentials`，不把密码作为工具输出或持久化文件。
随后持续把同一标签页的真实 URL 与 DOM 交给 `observe_learning_flow`，由它识别登录页、
匹配课程清单中精确ID/名称、检查班级、读取目录、分流测验/视频，返回下一项页面操作计划。
若课程列表尚未打开，返回页面实际可见的新版课程/课程/搜索导航；目标课程URL已由用户提供时可直接使用。
整个流程不建立另一份HTTP会话，不转移cookies，不启动新浏览器。

登录页返回现有页面的账号密码控件，控制器填入用户已提供的凭据；服务不接收或回显密码。
点击前调用 `mark_flow_login_attempt`，一次尝试后必须独立回读，不盲目重复登录。
协议接受和验证码属于当前操作确认门槛，不由代码自动同意或绕过。
章节测验使用prepare/release/confirm工具；视频用正常UI播放及真实媒体采样工具。
`learning_flow_progress` 返回当前课程、平台目录计数和历史记录，不把已规划步骤算作已执行。
`defer_learning_item` 按用户要求记录卡住的章节/课程并向后推进；被跳过不等于完成。
登录和多终端等账号级阻塞不通过换课绕过。

注意：这是宿主浏览器控制器协作的MCP链路，不是服务器内置的独立浏览器机器人。
验证新工具注册及本地状态机不代表真实平台端到端验证；以实时 `capabilities.version`、
`learning_flow` 字段和实际回读为准。服务重载前仍是旧工具集。

## 单会话任务执行

课程默认采用与考试相同的浏览器控制器协作模式，不再调用独立无头 Edge。
旧的独立浏览器测验和视频入口及其实现已移除，不自动回退或恢复。
HTTP 账号清单功能仍保留，但此流程不使用重复登录或导出浏览器 cookies。

1. 用已有浏览器扩展读取现有 Edge 标签页的 URL、DOM，调用
   `attach_course_browser(tab_id, browser_id, page_url, html_source)`。服务内只绑定一个标签页；切课仍用该标签页。
2. `observe_course_browser` 接收该标签页最新 DOM（目录或测验 iframe 的实际 URL 和 HTML）。
   已判分题只读取、跳过。密文字体、未知结构或题目缺失均阻止作答，不在页面注入解码脚本。
3. 完整审阅后 `prepare_course_quiz` 校验既有 v2 答题规则。控制器用正常可见控件选择答案，
   再采集新 DOM 交给观察接口；`release_course_quiz` 检查内容未变及实际已过去随机2–5秒。
   一份计划仅放行一次，平台提交由当前页面正常控件执行，不能重放结果不明的计划。
4. 提交后独立读取判分页交给 `confirm_course_quiz`。只有完整逐题对错、得分与满分值才更新确认缓存。
   不完整判分保持待确认、不重复提交；不把计划成功当作已完成或满分。
5. 视频同样由现有页面正常播放，控制器用只读 DOM 获取至少两次实际媒体时间、
   实际解码帧数、单调观察时间、静音和1倍速状态，交给 `inspect_course_media`。
   该工具不会播放、拖动、伪造前台状态或上传进度；媒体推进不等于平台任务点完成。
   任务点完成还须直接读取平台标记或目录增量。

多终端警告会锁住本轮流程，不自动忽略、不继续切课。先在原浏览器处理冲突，再重载服务重新绑定。
修改磁盘代码不刷新当前 MCP 进程。重载后须以 `capabilities.course_transport` 的
`legacy_independent_browser_disabled=true` 和新工具注册为准；未重载前不要调用旧浏览器工具。

## 调用

先调用 `capabilities`、`route_learning_task(task_type)`。
`task_type` 支持 `course`、`quiz`、`video`、`deadlines`、`exam`；
前四种归入课程任务，最后一种归入考试。

课程测验使用上方单会话观察与审阅流程；仍为密文字体时跳过等待支持，不猜字。
视频通过现有页面真实播放并返回媒体采样证据。
ICS 导出需要日历客户端导入，不等于主动提醒服务已运行。
账号凭据只保存在服务内存，不写入源码或配置。

考试默认使用浏览器协作模式，无需 CDP：现有浏览器控制器通过允许的只读 API
采集当前 URL 与 `document.documentElement.outerHTML`，交给 `attach_exam_browser`。
MCP 返回结构化题目及精确 `verified_cache`，由 `prepare_exam_browser_step` 校验答案并生成选项状态与操作计划。
浏览器控制器执行正常选择，再调用 `wait_exam_browser_save` 随机等待2–5秒并重新校验审阅，
取得 `save_ready=true` 后重查题目与选项，再点击“下一题／整卷预览”，导航回读原题或读取整卷预览，
交给 `confirm_exam_browser_draft` 比较已保存答案；之后 `refresh_exam_browser` 读取下一题。
`exam_browser_progress` 返回已验证数量，最后 `audit_exam_browser_preview` 比对整卷。
MCP 不直接控制扩展，不自动获取 cookies 或执行页面脚本；保存证据依赖控制器提供的独立回读。
计划生成不等于页面已操作，勾选状态不等于平台已保存。

可选 CDP 模式仍保留：`route_learning_task('exam', transport='cdp')` 返回
`attach_exam_session` → 审阅 → `run_exam_question_loop` → `audit_exam_preview`。
CDP 连接需要明确配置的本机 endpoint；服务不扫描端口或复制账号资料。
本服务不执行最终交卷，原定剩余 50 分钟交卷已取消。

答案优先使用当前题干和全部选项指纹完全匹配、未被拒绝的 `platform_confirmed` 缓存；
未命中由当前会话模型分析，不确定、证据冲突或缺少课程依据时才联网检索，结合来源复核。
旧缓存及部分得分不属于已验证命中。未命中时考试审阅须有 `model_reviewed=true`、`certain=true` 和 `basis`；
联网辅助时须提供 `used_web_search=true` 与非空 HTTPS `sources`。模型和搜索由当前会话负责，MCP 负责约束校验。
多选逐项审阅选中和排除理由，并检查指定数量。
曾判错或部分得分的组合不直接复用；保存成功不代表答题正确。
真实平台连接状态以实际工具回读为准，本地启动成功不代表考试已连接。

CDP 保存同样实际随机等待2–5秒并重查当前题。所有多选都检查选中和排除理由，包括恰选两项；
题干数量要求相互冲突时停止。已拒绝组合在缓存查询和保存入口均被拦截，部分得分不推断完整答案。
最终交卷须另获用户明确指示；平台开放评分后，读取实际评分页给 `inspect_exam_grading_html`。
只有完整逐题对错与得分一致确认满分时才报告100%；预览一致或评分未开放时不作满分结论。
该评分解析器仅识别明确的题目对错标记与得分，不对未支持的页面结构猜测评分。

## 多选审阅 v2 与错题记录

课程和考试共用同一审阅校验器。未命中精确平台确认缓存的多选，必须提交
`policy_version: 2`、`question_scope`、`scope_basis` 和覆盖全部选项的 `option_checks`。
`question_scope` 取 `general_definition` 或 `course_enumeration`。
“一般有用/现实中正确”不证明“本节列举/本题要求包含”；不以全选、漏选修补或评分倒推代替证据。

每项检查包含 `verdict: include|exclude`、`addresses_exact_question: true`、
`general_truth_only: false`、`evidence`、`evidence_kind`。
课程列举题必须使用 `evidence_kind: course_material`，附 `source`（HTTPS）和
`locator`（段落/页码/视频时间位置）。通用定义可使用 `model_definition` 或有 HTTPS 来源的
`authoritative_definition`。第三方答案库只做线索，不单独证明课程选项边界；有疑问整题暂停。
字段完整仅证明完成了证据审阅声明，MCP 不会自动判断材料真伪或保证正确率。

`get_wrong_questions` 读取输出目录下 `studyflow-mistakes.json`：保存完整题干、选项、
错误组合、逐题得分及原审阅依据，不保存密码、cookies 或签名链接。
判错组合在课程及考试入口拦截，读取题目时附错题提醒。未知/缺失判分不作为判错。
仅有平台明确判分才能确认正确组合；错题记录不会生成未经核验的“正确答案”。
已经显示判分的课程测验在读取/提交入口阻止重复提交。
磁盘升级需重载 MCP 后生效；旧进程不因文件修改自动更新。
# 1.6.6 卡片识别修正

章节卡片不再假设只有两个或固定顺序。读取全部可见 `dct数字` 控件，按标题和 active/aria-selected 区分学习目标、视频、章节测验；学习目标先阅读，再通过实际视频标签进入播放器。`下一节` 可能切卡片或章节，每次操作后重新识别，不连续盲点。

没有媒体时先检查当前卡片及嵌套 iframe，普通内容页不判为播放器故障；视频内页空白只能记加载状态未知。只有真实控制请求失败才记连接故障。重复或冲突控件不自动选择，验证及账号守卫不变。此更改需要服务重载，磁盘版本不代表运行版本。


## 宿主自动恢复约定

已安装技能 `C:/Users/xyh/.codex/skills/chaoxing-exam-mcp/references/autonomous-recovery.md` 定义 Edge 诊断、原配置启动、一次页面恢复、重新登录、门户导航、任务核对与静音播放。诊断入口为同技能 `scripts/diagnose-edge.ps1`。这些由活跃宿主执行；服务没有新增后台驱动，当前网页读取超时仍需现场验证。


## 1.7.0 登录、门户与截止信息

固定登录入口为 https://passport2.chaoxing.com/login；已登录时复用原页，不重复登录。
classify_learning_portal 区分 i.mooc.chaoxing.com/space/index 与 i.chaoxing.com/base，
按各自导航优先级匹配真实控件，不拼接带签名的课程入口。
学习目标卡片跳转到实际视频或章节测验；跳过不计作完成。

inspect_homework_browser 现在解析明确的非空作业链接、状态和截止标签；
尚未覆盖的结构返回未知，coverage=observed_page_only，不据此宣称已经检查全部作业。
evaluate_homework_deadlines 使用北京时间、默认24小时窗口，排除已提交/已完成，区分临期与逾期。
日期没有明确时分时不猜截止时刻。ICS 使用UTC时间并转义文本。
这些是库存解析与提醒候选；尚未启动持续监控或发送通知。

章节观察一次返回当前 DOM 的全部题目；已发现分页、总数冲突或缺题时阻止准备提交。
complete_count_verified 明确区分已核对题目总数与仅提取当前页面。
独立批量HTTP取题尚未完成真实接口接入，不能把 DOM 解析称作HTTP接口已跑通。
考试继续逐题暂存并在整卷预览结束，不最终交卷。

验证：23项本地测试通过；独立stdio进程注册46个工具。
桌面已连接进程仍须以实时capabilities为准；新工具注册需重载，不借库存热更冒充整服务更新。
浏览器官方恢复后可以导航，但真实登录页DOM读取仍超时；登录、视频、评分与整卷预览尚未完成本轮真实验证。

## 1.9.1 直接课程浏览器（2026-10-20）

课程默认路线为用户明确启动的 requests 登录 + MCP 内单线程 Playwright。
调用 import_accounts → login_accounts → start_course_browser(channel="msedge") →
goto_course_page → inspect_owned_page/find_chapter_links → open_course_chapter。
页面和登录属于 MCP 进程而非聊天轮次；服务重启需要重新导入账号并登录，密码和 cookies 不落盘。
重复 start 复用原实例；不同账号/浏览器需显式 stop，禁止覆盖句柄。
browser_status 返回真实存活检查、server_pid、generation、worker_failed 和提交状态。

选择卡片以 inspect_owned_page 返回的实际 dct 控件为准，不能假定 dct2 永远是测验。
页面可能异步加载，cards 为空或 loading=true 时先独立观察，不宣称控制失败或没有任务。
control_owned_video 仅通过播放器控件静音1倍播放/暂停及读取真实采样，不拖动或写入进度。
已完成任务点跳过；媒体推进/结束不等于平台确认完成。

测验 observe_chapter_quiz → prepare_course_quiz → 新观察 → release_course_quiz →
submit_chapter_quiz。选择使用实际 radio/checkbox 行和 class/aria/native checked 回读，
完全匹配所有选项后等待2–5秒，只点击唯一可见“提交”控件一次。超时或结果未知不重放。
题量采用全卷“题量/总题数”，不把分题型“共N题”当总数。保留分页/缺题/冲突阻断。

refresh_direct_implementation 是显式受控代码更新：只在工作线程空闲且没有未知提交结果时，
保留浏览器、线程、会话和代次更新课程后端及解析器。丢弃未提交计划须显式传
discard_unsubmitted_plan=true，再重新观察和审阅。不会借库存热更新伪装整服务更新。
新工具注册和首次加载仍需重新连接 MCP；仅磁盘版本变化不等于桌面已加载。

提交确认支持课程主页面的“确认提交？”弹窗，只在提示所属容器内点击一次。
read_chapter_quiz_grading 独立回读已有评分；未知提交结果保留计划并阻断重放和关闭。
评分页通过课程/测验身份、逐题 ID、题干、已有选项及实际提交答案核对，兼容只读版式。
新版没有逐题满分时，用逐题得分总和、总成绩和总满分交叉核对；部分得分不提升缓存可信度。
首次播放器“知道了”提示通过正常界面关闭，保持静音 1 倍速，不强行穿透遮挡层。

视频内随堂题通过 observe_video_question 读取完整题干/选项与当前页面 token；
answer_video_question 只接受当前模型明确审阅的确定答案，通过真实 radio 和提交控件操作。
核对题目未变化、选择实际登记后等待2–5秒，只提交一次。结果不明不重放；
恢复播放不冒充明确判分，最终视频任务以平台完成标记和目录增量确认。


## 共享 QQ SMTP 与多账号通知

可选：先调用 `configure_smtp_sender(sender_email, smtp_auth_code)` 设置一套发件邮箱和 SMTP 授权码（不是邮箱登录密码）。然后 `import_accounts` 每条记录填写 `account_id`、`username`、`password`，并可选填写不同的 `notify_email`。不填收件邮箱的账号跳过邮件通知；需要修改时调用 `bind_account_notification_email`。也可先绑定收件人，之后配置共享发件人。

凭据只保存在服务进程内存；重启后重新输入。不要把真实密码、授权码放进 README、mcp.json、日志或任务状态。不同账号的学习浏览器和进度独立，当前服务仅串行控制一个账号。

视频或页面检测到人脸/二维码验证时，本地事件服务进入15秒验证等待。仅截取唯一明确 QR 标记的可见图像；同一二维码去重，仅平台明确过期时点击唯一可见刷新控件，待新二维码出现后继续发信（最短30秒去抖），验证通过后停止。缺失二维码或控件歧义时仅发文字提醒、保留现场，不绕过验证。普通视频仍按事件及预计结束时间检查，不每15秒轮询视频。

SMTP 通过 `smtp.qq.com:465` TLS 发信；邮件操作及图片保留内存，发送在独立线程中执行。失败/未知不重放同一码；`smtp_notification_status(account_id)` 返回缓存结果，`smtp_accepted` 仅表示 SMTP 接收。邮件配置未完成时，课程仍可使用，验证改由聊天提示。

参考项目与许可证见 THIRD_PARTY_NOTICES.md。当前自动化测试覆盖账号绑定、去重、刷新条件、TLS 邮件结构等；真实课程二维码适配与邮箱投递还需用实际账户验证。


## 默认自动浏览器

StudyFlow 服务连接启动时自动打开一个有窗口的独立 Edge，未输入账号时显示超星官方登录页。输入账号并登录后复用同一实例，不另开浏览器；播放时按既定设置静音、1.25倍速和最小化。同一 MCP 进程只保留一个学习账号浏览器；不同客户端进程各自独立。仅需协议或无浏览器测试时可设置环境变量 `STUDYFLOW_AUTO_BROWSER=0` 关闭自动启动。服务断开时正常清理自己拥有的浏览器。


## 静默启动与多账号

启动默认添加 `--start-minimized` 并通过真实窗口接口最小化，不最大化、不抢前台。信息收集完后调用 `setup_learning_accounts` 导入账号和可选邮箱配置、登录首个账号；剩余账号按输入顺序保留。当前账号任务完成后 `login_accounts([下一账号ID])` 或 `activate_learning_account` 复用同一个浏览器进程，先关闭旧账号上下文再创建隔离上下文，不同时打开多账号窗口。验证待处理、测验计划待提交或提交结果未知时禁止切换。原生窗口建立瞬间是否出现取决于系统与浏览器，不承诺完全无窗口闪现。


## 两种模式统一选择

登录前调用 `configure_course_browser(mode="headless")` 选择完全无窗口，或 `mode="minimized"` 选择有窗口但最小化（默认）。也可在 `setup_learning_accounts` 中传入同名 `browser_mode` 参数。启动默认值可用环境变量 `STUDYFLOW_BROWSER_MODE=headless|minimized` 设置。

两种模式都是同一份 MCP、同一个账号串行队列，不为每个账号再启动浏览器；切换账号只替换隔离上下文。模式切换必须在登录前进行，先关闭旧实例再启动新模式，避免并存。活动账号/验证/提交现场不会被模式切换静默销毁。`owned_window_status` 可只读核对窗口状态和上下文/页面数。


### Incremental parallel account admission

After importing a new account, call `add_parallel_learning_accounts(accounts=[{"account_id":"new-account","courses":["Exact course name"]}], browser_mode="headless", rate=1.25)`.
An existing supervisor reuses unchanged account queues and starts only newly admitted accounts. Repeated start/setup calls use the same admission path; queue or mode conflicts are reported without replacing existing workers. New logins never activate in the parent browser. The supervisor remains available after initial workers finish. A new account is not a retry: `ACCOUNT_NOT_REGISTERED` points to admission, while recovery still requires a confirmed terminal worker and no review/submission attempts. OS lease files must not be deleted to bypass a live account owner.

This is a source update; running older processes require a suitable explicit update opportunity. It does not restart browsers or load itself into another host automatically.


### Long-running recovery and optional SDK bridge

Transient read failures (empty/loading frames, detached frames, execution context replacement, read timeouts) use per-task local 1/2/4 second backoff. Verification, closed browsers, pending reviews/submissions and unknown outcomes do not use this retry path. Quiz review is a host-model responsibility, not a user intervention notice. Existing safe pause recovery remains event driven.

`runtime_health` returns cached diagnostics without page/credential reads. `outputs/studyflow-lifecycle.jsonl` records process start, transport return/EOF, exception type and shutdown; it rotates at 256 KiB and omits exception messages/arguments. Abrupt OS kills may leave no shutdown entry; absence alone is not a diagnosis.

For a host-independent loopback bridge, explicitly launch `python -B -m chaoxing_learning_mcp --bridge --port 8791` in a suitable persistent process environment. The official MCP SDK multiplexes requests; account locks are separate. Ordinary TCP client disconnects do not close the owned MCP. No auto-restart, disk-credential loading, browser prelaunch or unknown-submission replay occurs. This optional entrypoint is not activated merely by updating source. Never replace an active service/port to apply it mid-task.


### 1.9.2 consolidation

2026-10-22 runtime fixes: read-only requests ProxyError/Timeout recovery is bounded per account; TLS/verification/submission guards retain priority. Grading normalizes class/chapter URL aliases while retaining full quiz/question/answer checks. The explicit `recover_graded_parallel_account` compatibility tool independently verifies complete platform scores before retiring only an old stuck worker through its SDK lifecycle. It never replays submissions or restarts healthy accounts; the recovered worker starts a new hourly reporting interval. Ordinary pending/unconfirmed outcomes remain protected. Actual ended playback without platform completion remains unresolved, rather than being counted as complete.

- Media initialization waits up to 8 seconds for real metadata in the selected frame before the existing 1.25 adapter; detach/loading uses bounded same-account recovery. Actual media errors remain explicit and never silently fall back to 1x.
- Account switching clears the binding dictionary without replacing its type, and preserves the configured 1.25 default.
- `submit_reviewed_chapter_quiz` mechanically normalizes an explicitly reviewed answer set, enforces the original evidence policy, waits the required interval, obtains a fresh observation and submits once. It never invents answers or upgrades uncertainty.
- Stage notices list newly confirmed videos, each quiz score (and individual item points when captured), verified remaining task counts and remaining target courses. Unknown counts stay unknown.
- 22 obsolete/diagnostic tool entrypoints are omitted from default discovery. Internal adapters and tests remain intact. Set `STUDYFLOW_ENABLE_LEGACY_TOOLS=1` only for explicit compatibility needs; exams, SMTP, face screenshots and submission guards remain enabled.
- The SDK bridge supports `__tools__` for actual tool-discovery verification. Starting this service does not import credentials or resume a paused course.
