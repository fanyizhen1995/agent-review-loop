# 参数说明

入口为项目目录中的 `project.toml`，也兼容直接传入 TOML 文件。以下是字段分工；可直接运行的完整虚构配置见 `examples/projects/example-gitlab/` 和 `examples/projects/example-github/`。

## 项目、账号与访问

| 表 | 关键字段 | 作用 |
| --- | --- | --- |
| `project` | `name`、`language`、`source_dirs`、`source_extensions` | 项目标识、语言与源码范围 |
| `project` | `hot_files`、`forbidden_files`、`test_commands` | 人工处理文件、禁止执行方修改的文件、测试说明 |
| `project` | `internal_terms`、测试文件及共用工具字段 | 写作与测试约定 |
| `forge` | `kind`、`base_url`、`project`、`repository` | forge 类型、站点、项目号或 owner/repo、仓库展示名 |
| `labels` | `source` | 本流程管理条目的来源标签 |
| `accounts` | `reviewer`、`fixer`、`user`、`mention` | 三方身份与用户提及 |
| `accounts` | `legacy_fixer_username`、`user_note_since` | 历史共用身份的切换边界 |
| `credentials.<role>` | `file` 或 `env`，可加 `dotenv_file` | 凭据来源；`role` 为 reviewer/fixer |
| `transport.<role>` | `mode`、`hosts` | direct 或 ssh_chain，后者按顺序列主机 |
| `branches` | `base`、`protected`、`forbidden_prefixes`、`work_prefixes` | 基准及分支操作边界 |

`project.name` 只能含字母、数字、下划线、点和连字符，首字符不能是点或连字符。GitLab 项目编号为正整数，GitHub 为 `owner/repo`。forge 站点 URL 不得包含账号密码、查询参数或片段。凭据表不能填写额外 token 键。

## 工作流

`[workflow]` 中 `issue_types` 列出允许的问题类型，`language` 指定输出语言，目前模板只支持 `zh`。

每个 `[[workflow.verify]]` 包含以下字段：

| 字段 | 含义 |
| --- | --- |
| `name` | 步骤的稳定名称 |
| `run` | 执行命令 |
| `builtin` | 内建验证，如 `regression`，用于基准失败与修复通过证据 |
| `for_types` | 适用问题类型；空列表适用于全部类型 |
| `when_paths` | 适用路径 glob；按改动路径选择步骤 |
| `lock` | 需要串行使用的资源名称 |
| `description` | 验证目的和证据说明 |
| `rerun_by_reviewer` | 是否由评审方重新执行 |
| `reviewer_run` | 评审侧命令，省略时采用 `run` |

命令支持运行时的 `{file}`、`{branch}`、`{sha}`；`{kit}` 指向角色工具目录。项目 `hooks/` 相对角色安装目录，不能依赖套件源码目录。验证步骤与项目资源必须一起审查。评审指定测试路径时必须配置 `reviewer_run`，并通过 Bash 的 `${args[@]}` 消费参数；未配置时明确报错，避免漏跑回归测试。`{file}` 来自 `ARL_FILE` 或首个测试参数，分支、提交与改动路径由轮询进程传入，手动运行可通过 `ARL_BRANCH`、`ARL_SHA`、`ARL_ISSUE_TYPE`、`ARL_CHANGED_PATHS` 指定；代码根目录的实际 Git 提交优先用于 `{sha}`。

`[workflow.metrics]` 支持 `complexity_cmd` 和 `test_ratio_limit`。复杂度命令自行适配项目语言，公共 `complexity.py` 只分析 Python。测试比例超限须解释原因，不能用数量代替测试价值判断。

`[workflow.merge]`：

| 字段 | 含义 |
| --- | --- |
| `enabled` | 启用自动合入 |
| `approvers` | 成员账号白名单；空列表从可写成员中选择 |
| `github_method` | merge、squash 或 rebase |
| `remove_source_branch` | 合入后尝试删除源分支 |
| `approve_phrases`、`revoke_phrases` | 自定义同意与撤回短语列表，空列表沿用默认词组 |

同意来自成员评论；最近有效同意或撤回决定当前状态。执行方和评审机器人不能作为同意人。分支保护和当前 HEAD 检查继续生效。

`[workflow.register]`：

| 字段 | 含义 |
| --- | --- |
| `enabled`、`cron` | 启用登记及独立 cron，使用主机本地时区 |
| `branch` | 登记提交的目标分支 |
| `format` | none、changelog-md、hook，或兼容的 agents-tasks-json 项目登记格式 |
| `file` / `changelog_file` | changelog 目标，默认 `CHANGELOG.md` |
| `hook` | 已安装 hooks 下的项目登记脚本相对路径 |
| `files` / `paths` | hook 可修改的仓库内相对文件白名单 |
| `tasks_file`、`progress_file`、`id_prefix` | 兼容项目登记格式的路径和任务 ID 前缀 |

hook 路径相对已安装的 `reviewer.install_dir/hooks/`，例如 `hook="register.py"`。程序直接执行该文件，工作目录为评审安装目录，通过标准输入接收待登记 MR 数组 JSON，标准输出必须为“仓库相对路径到完整文件文本”的 JSON 对象。脚本需可执行，60 秒内完成；空对象表示无文件变更。输出路径必须在 `files/paths` 白名单内，非零退出或无效输出会停止登记。`--dry-run` 也会执行该生成脚本，因此 hook 必须只生成输出，不自行写 forge 或本地状态。

白名单和输出路径不得使用绝对路径、反斜线或路径穿越；不同登记文件不能重名。通用项目优先选择 none、changelog-md 或 hook。旧格式由兼容层读取，新配置使用 `workflow`。

## 评审端

| 表 | 字段 | 作用 |
| --- | --- | --- |
| `reviewer` | `repo`、`install_dir`、`ref_dir` | 仓库、工具和规则引用目录 |
| `reviewer` | `runs_dir`、`run_root` | 状态日志、独立工作区根目录 |
| `reviewer` | `skill_dir`、`relative_install_dir` | 技能位置和相对命令路径 |
| `reviewer` | token 及私有配置路径 | 生成读取权限限制 |
| `reviewer.git` | `fetch_url`、`pre_fetch_cmd` | Git 地址与预取命令数组 |
| `reviewer.git` | `base_branch`、`base_ref`、`source_ref_prefix` | 分支展示与实际引用 |
| `reviewer.tests` | `command`、`full_command`、`node_command`、`node_path` | 兼容测试脚本与说明；新增验证使用 workflow |
| `reviewer.scope` | 源码、设计依据、禁止改动路径 | 项目范围说明 |
| `reviewer.cron` | `schedule`、`path`、`python` | 轮询时刻、PATH 与 Python |

`reviewer.limits` 默认值：3 个并发槽，每小时 12 次，同标识最多 2 次；Claude/Codex 超时 1800/3600 秒；复核和轻量预算 2/1 美元；一般、MR 和用户评论静默期分别为 20/5/3 分钟。计数和超时必须为正整数，静默期为非负整数，预算为有限正数。

## Hermes 执行方示例

`fixer.adapter="hermes"` 选择示例适配器。`host`、`hermes_home`、`repo`、`review_dir`、`ref_dir`、`rules_file` 和 `skill_dir` 确定安装位置；运行脚本、通知状态、scratch 和清理边界也由 fixer 字段配置。 `ssh_host` 可选，填写 SSH 连接用的主机别名（与显示用的 `host` 不同时）；`hermes_cli` 可选，填写执行端 hermes 命令路径（非交互 SSH 的 PATH 不含该命令时使用，默认 `hermes`）。 `max_pending_issues` 为"待评审"issue 的上限，`max_open_issues` 为执行方打开状态 issue 的总上限（默认 30，0 表示不限），任一达到上限时暂停自主发现和提新 issue，先推进现有 issue；`max_discover_per_run` 为每次运行最多的自主审视项数。

`fixer.approvals` 配置模式、超时以及命名空间和推送禁止项。文字规则与 deny 清单都应覆盖实际环境中的命令形式，不得绕过匹配。`fixer.delegation` 配置 worktree 隔离和子任务上限。`fixer.cron` 配置分派、通知、清理任务的名称、间隔和投递渠道；名称用于幂等更新。

`fixer.deploy` 保留环境说明及旧配置兼容字段；公共套件不提供项目镜像构建脚本。构建、部署、证书等项目需求由 workflow.verify、hooks 和 assets 承载，不能把私钥或 token 当作资产。

`[project_files]` 的 `reviewer`、`fixer` 为资源子目录数组，默认包含 `hooks` 和 `assets`。资源复制到对应角色的生成根目录；符号链接和越界路径应在接入检查中拒绝。可选 `[project_files.fixer_ref]` 用名称到项目内相对资源路径的映射，将兼容资源另行安装到执行方 ref_dir。
