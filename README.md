# agent-review-loop

agent-review-loop 将执行方 agent、独立评审、成员同意合入和结果登记组织为可配置流程。公共代码只依赖 Python 3.11+ 标准库，支持 GitLab 与 GitHub；Hermes 是随套件提供的执行方适配器示例。

执行方先提交问题与修复范围，范围确认后创建 MR/PR。评审方按项目工作流检查证据、运行配置的复核命令并回复评论。项目成员同意且评审通过后，定时程序检查合入条件；登记任务按配置记录已合入结果。

```mermaid
flowchart LR
    A[发现问题] --> B[问题评审与范围确认]
    B --> C[执行方修改与验证]
    C --> D[独立评审]
    D -->|需修改| C
    D -->|可合入| E[成员同意与合入检查]
    E --> F[登记结果]
```

| 目录 | 内容 |
| --- | --- |
| `examples/projects/` | GitLab、GitHub 虚构项目目录 |
| `kit/` | 配置、工作流、forge、传输和渲染公共层 |
| `reviewer/` | 轮询、评审提示词、权限与登记 |
| `fixer/rules.base.md.tmpl` | 通用执行规则 |
| `fixer/adapters/hermes/` | Hermes 任务、配置和脚本示例 |
| `install/` | 渲染、安装预览和环境检查 |
| `tests/` | 离线行为与权限边界测试 |
| `docs/` | 接入、参数、架构与适配器契约 |
| `build/` | 不纳入版本管理的本地生成结果 |

快速验证虚构示例：

```bash
python3 install/render.py examples/projects/example-gitlab build/example-gitlab
python3 install/doctor.py examples/projects/example-gitlab --offline
python3 install/install_reviewer.py examples/projects/example-gitlab build/example-gitlab --dry-run
python3 install/install_fixer.py examples/projects/example-gitlab build/example-gitlab --dry-run
python3 -m unittest discover -s tests
```

实际接入时将示例目录复制到独立的私有项目仓库，再填写路径、账号、凭据引用、验证命令和项目规则。入口可接收项目目录，也兼容单个 TOML 文件；目录接口支持规则覆盖及 hooks/assets。详见[配置指导](docs/setup-guide.md)、[参数说明](docs/parameters.md)、[适配器契约](docs/adapters.md)和[发布检查](docs/publishing.md)。

凭据只通过文件或环境变量读取。执行规则和审批禁止清单共同限制共享主机操作；执行方不能 approve 或合入。安装预览不访问远端，不更新目标文件或定时任务。
