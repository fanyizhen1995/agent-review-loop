# 接入助手提示词

将下面内容交给负责项目接入的助手，并补充自己的私有项目目录。先离线生成可审查产物，再在获得项目部署授权的环境中安装。

```text
请为我的项目配置 agent-review-loop。公开套件是通用流程，项目差异必须放到独立私有项目目录，不能写回公共模板或示例。

1. 读取 README.md、docs/setup-guide.md、docs/parameters.md 和 docs/adapters.md。
2. 按 forge 选择 examples/projects/example-gitlab 或 example-github 作为结构示例。确认项目源码、测试命令、身份、工作区和凭据引用；不要索要或输出 token 内容。
3. 使用目录入口 project.toml，项目规则写 rules-overlay.md 和 standards-overlay.md，业务验证放 hooks，资源放 assets。
4. 使用 workflow.verify 定义验证、适用类型和路径、资源锁及评审复核命令；使用 workflow.metrics 定义度量，workflow.merge 定义合入，workflow.register 定义登记。避免新增项目专属公共代码。
5. 执行方身份不能 approve 或合入，评审 CLI 只允许约定的评论、标签和关闭操作。核对分支保护、共享主机边界和审批拒绝清单，不绕过拒绝。
6. 渲染项目目录，执行 doctor --offline 和双安装器 --dry-run。核对输出规则、权限、资源、cron、文件摘要和所有目标路径。
7. 运行离线测试及公开发布检查，报告实际验证结果和未验证条件。不要把部署凭据、内部地址、真实账号或运行日志放入公开仓库。
8. 根据项目已有授权安排联机检查和正式安装。在自己的环境验证范围确认、修复、独立评审、成员同意、合入、登记及通知去重，保留失败证据。
```
