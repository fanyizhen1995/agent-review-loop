# 评审配置约定

运行配置来自 `AGENT_REVIEW_CONFIG` 或脚本旁 `project.toml`；配置入口接受项目目录和 TOML 文件。导入 forge_cli 不读取凭据、不发请求。

工作流使用 workflow.verify、workflow.metrics、workflow.merge 和 workflow.register。旧格式输入由兼容层映射；新项目只写 workflow。执行与评审要求由同一工作流生成，项目附加规则使用项目目录 standards-overlay.md。

reviewer 路径定义工作区、工具、规则、状态、隔离 worktree 和凭据禁止读取范围。reviewer.git 决定拉取方式；GitHub 使用 pull 引用，GitLab 使用配置的 source_ref_prefix。reviewer.limits 约束并发、重试、预算、超时和静默期。

GL_READONLY、AGENT_REVIEW_READONLY、DRY_RUN 任一启用均阻止 forge 写操作。CLI 不开放合入与登记；定时程序使用独立权限上下文，子进程不继承授权。

详细字段见 docs/parameters.md，项目资源与命令约定见 docs/adapters.md。
