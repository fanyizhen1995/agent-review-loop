# 执行方适配器契约

适配器连接通用流程与具体 agent 的任务系统。公共规则和工作流不依赖某一种 agent；仓库内 `fixer/adapters/hermes/` 是可参考的 Hermes 实现。目前安装器支持该示例，新增产品需同时实现渲染与安装入口，不能仅改 `fixer.adapter` 宣称已接入。

## 输入与目录

配置以校验后的 project、forge、accounts、credentials、branches、workflow 和角色路径为输入。凭据只传来源引用，禁止渲染 token。项目目录可追加 `rules-overlay.md`、`standards-overlay.md`，并提供 hooks/assets。单 TOML 输入不加载相邻覆盖文件。

| 源文件 | 职责 |
| --- | --- |
| `fixer/rules.base.md.tmpl` | 通用行为、状态交接和权限边界 |
| `fixer/adapters/<名称>/SKILL.md.tmpl` | agent 入口、环境事实与唯一规则引用 |
| `fixer/adapters/<名称>/dispatcher.md.tmpl` | 选择待办、冲突检查、分派与简报 |
| `fixer/adapters/<名称>/scripts/` | 状态探测、通知、清理、forge 调用辅助 |
| `fixer/adapters/<名称>/config/` | 产品专属受管配置片段 |

适配器应引用渲染后的 `rules.md`，不能复制另一套流程到技能或记忆。由 workflow 生成验证要求，以项目覆盖补充业务边界。评审标准与执行方必须看到相同的步骤名称、适用条件和证据要求。

## 生成产物

渲染根目录固定分为 `reviewer/` 与 `fixer/`。执行方至少提供 `rules.md`、规则入口、分派说明和所需脚本；配置读取使用 `AGENT_REVIEW_CONFIG` 或安装脚本旁的 `project.toml`。脚本通过公共 forge 与 transport 访问平台，保持两种 forge 的标准化字段，不在 agent 适配器重建 HTTP 凭据处理。

项目资源按 `[project_files]` 复制到角色产物根目录，默认两端包含 hooks/assets。可选 `[project_files.fixer_ref]` 将指定已渲染资源额外复制到 ref_dir，供旧规则路径兼容。命令 `hooks/check.sh` 相对执行方 `review_dir` 或评审方 `install_dir`；`{kit}` 为角色工具目录，`{file}/{branch}/{sha}` 在执行时替换。应完整安装所有被命令引用的文件，并维持可执行权限。不得把绝对机器路径写死到公共模板。

登记 hook 是独立生成协议：`workflow.register.hook` 相对已安装的 `hooks/`，直接执行而不通过 shell；标准输入为待登记 MR 数组 JSON，标准输出为仓库相对路径到完整文本的 JSON 对象。输出必须符合 files/paths 白名单。hook 在 dry-run 中也运行，应只生成输出；不得自行推送、写 forge 或改变状态。

## 调度与状态

状态探测须区分：等待评审、需修改、已确认但未确认范围、可开发、可合入、等待前置、文件冲突及已合入。只有 `review::已确认` 与 `scope::确认修复` 同时存在时开始开发；他人 open MR 修改的文件和热点文件按规则避让。

分派任务仅在有状态变化时唤醒模型，限制同轮新发现数与并发数。子任务使用分配的独立 worktree，不能重用其他任务的工作区。通知首轮建立静默基线，此后只报告用户评论回复与可合入状态，并通过持久状态去重。清理仅操作本任务创建、满足年龄且无进程使用的资源。

Hermes 的 `cron-jobs.json` 为 `{"jobs": [...]}`，包含分派、通知、清理三项。分派使用 `prompt_file`、`monitor_script` 与 `workdir`；通知、清理使用 `script`、`no_agent=true`。各项名称和间隔来自 `fixer.cron`，按名称幂等识别。

## 安装边界

安装器应先预览映射、摘要和受管配置，再执行备份及受控更新。`--dry-run` 不连接远端、不修改目标文件、不更新 cron。正式安装保留非受管设置和任务，不得整份覆盖用户配置。

Hermes 只管理 `approvals`、`delegation` 和 `agent.max_turns`。不支持的复杂 YAML、重复受管键、未知 cron CLI 格式应明确失败。同一 Hermes home 发现其他项目时拒绝覆盖；多项目使用独立 home/profile。新适配器应给出等价的隔离与配置保留措施。

## 权限与失败

执行方不能 approve、合入、修改评审已确认标签或绕过审批拒绝。共享主机禁止重启、停止系统服务、修改 `/etc` 以及操作其他任务的容器、进程和 Pod。项目 hook 的权限不得超出角色边界。

验证失败保留步骤、退出状态与必要输出；环境不足写明缺少条件。凭据和敏感响应不得进入日志。合入与登记由独立定时程序的权限上下文执行，适配器不获得同等写权限。

## 接入验收

至少验证状态门槛、冲突避让、首轮通知静默、去重、清理边界、凭据不回显、禁写权限和 dry-run 无副作用。渲染两个虚构 forge 示例，核对规则、验证步骤、项目资源和安装目标。离线测试使用替身；真实 agent CLI、账号权限和部署冒烟在项目自己的环境中单独记录。
