# 项目接入指导

项目差异放在独立项目目录。公开示例只展示配置结构，账号、地址、路径和命令均为虚构值。开始前准备 Python 3.11+、可用的模型命令、仓库工作区，以及分离的 `review-bot`、`fixer-bot` 和成员 `maintainer-a` 身份。

## 1. 准备项目目录

按 forge 选择 `examples/projects/example-gitlab/` 或 `examples/projects/example-github/`，复制到自己的私有项目仓库。项目目录的核心布局为：

```text
my-project/
  project.toml
  rules-overlay.md
  standards-overlay.md
  hooks/
    smoke.sh
  assets/
```

`project.toml` 是入口；规则覆盖文件和资源目录均为可选。`rules-overlay.md` 与 `standards-overlay.md` 用于追加项目约束，不应削弱通用权限和交接规则。hooks/assets 默认复制到两端产物根目录，可用 `[project_files]` 按角色指定。传入单个 TOML 文件时保持兼容模式，不自动采用其相邻规则和资源。

## 2. 配置身份、路径和权限

填写 `forge.kind/base_url/project`。GitLab 的项目号是正整数，如 `123`；GitHub 为 `example-org/example-github`。`accounts` 中分开指定评审、执行和成员账号；历史共用账号只用于识别已有条目。

评审端用 `reviewer.repo/install_dir/ref_dir/runs_dir/run_root` 指定工作区、工具和运行状态位置。Hermes 示例用 `fixer.host="build-host"`，执行目录在 `/home/agent/` 下。远端 `~/` 按 `fixer.home` 或 `hermes_home` 所在 home 展开。主机名和路径须替换成实际部署值。

凭据配置只填写 `file`、`env`，或 `dotenv_file` 加 `env`，不填 token 内容。凭据文件限制为账号本人可读写。`transport.<role>.mode` 为 `direct` 或 `ssh_chain`；后者的 `hosts` 按访问顺序列出，凭据通过标准输入传递。Git 拉取使用 `reviewer.git`，可独立配置拉取地址与预取命令。

执行账号只写自己的工作分支。评审 CLI 的写入范围限定为评论、自己的评论编辑、指定评审标签和关闭条目。合入和登记由定时程序的独立权限上下文完成；forge 分支保护仍须阻止执行账号直接推送默认分支。

## 3. 配置工作流

用 `workflow.issue_types` 和 `workflow.language` 定义问题类型与写作语言。每个 `[[workflow.verify]]` 描述一个验证步骤；用 `for_types`、`when_paths` 控制适用范围。`run` 给出执行命令，`builtin="regression"` 要求缺陷回归在基准分支失败、修改分支通过；`lock` 指定串行资源。`rerun_by_reviewer=true` 要求评审方复核，`reviewer_run` 可覆盖评审侧命令。

```toml
[[workflow.verify]]
name = "unit"
run = "python3 -m pytest -q tests"
rerun_by_reviewer = true

[[workflow.verify]]
name = "smoke"
run = "hooks/smoke.sh {branch} {sha}"
when_paths = ["src/**", "deploy/**"]
lock = "deploy"
```

`hooks/` 相对执行方 `fixer.review_dir` 或评审方 `reviewer.install_dir`。`{kit}` 指向所在角色的工具目录，`{file}`、`{branch}`、`{sha}` 在运行时替换。工作流命令属于受信项目配置；hooks 应自行检查环境、限定资源并在失败时返回非零状态。

`workflow.metrics` 配置复杂度命令和测试源码比例。`workflow.merge` 配置成员同意与合入方式。`workflow.register` 可关闭登记、只记事件、写通用 changelog，或调用限定文件白名单的项目 hook。完整字段见[参数说明](parameters.md)。

## 4. 离线渲染和安装预览

在套件根目录运行，项目路径可以是绝对路径：

```bash
python3 install/render.py examples/projects/example-gitlab build/example-gitlab
python3 install/doctor.py examples/projects/example-gitlab --offline
python3 install/install_reviewer.py examples/projects/example-gitlab build/example-gitlab --dry-run
python3 install/install_fixer.py examples/projects/example-gitlab build/example-gitlab --dry-run
```

检查生成的两端规则、验证步骤、权限、定时任务、资源文件和目标路径。预览会显示差异及文件摘要；执行端现有内容需正式安装时读取。渲染会替换受管产物，关闭选项后不会残留上次生成的对应文件。

## 5. 部署与试跑

在自己的环境检查账号权限、分支保护、模型可用性、Hermes CLI 兼容性、Git 访问和 cron 时区。`doctor` 联机检查会访问配置的 forge 与执行端；确认环境后运行。正式安装会备份受管文件，幂等更新带项目标记的任务，保留其他配置与任务。

先创建一条无业务影响的测试问题，验证范围确认、修复、独立评审、评论回复和通知去重。合入前检查成员同意、最新 HEAD、冲突和保护规则。启用登记后先运行 `register.py --dry-run` 预览，再确认仓库内仅预期文件发生变化。记录部署环境中尚未验证的条件，不能用离线测试结果替代部署证据。
