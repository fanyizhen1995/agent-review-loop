# Hermes 渲染约定

Hermes 是通用执行流程的示例适配器。通用规则来自 `fixer/rules.base.md.tmpl`，产品专属技能、分派任务、审批片段和脚本放在本目录。项目覆盖及 workflow 合入后生成唯一 `rules.md`。

脚本输出到执行方 `scripts/`，公共 kit 和项目配置随运行脚本安装。模板凭据字段只生成来源说明，不读取 token。审批 TOML 渲染后转为受管 YAML 补丁，只包含 approvals、delegation 和 agent.max_turns。

`cron-jobs.json` 包含分派、通知、清理三项，名称、间隔和投递来自 fixer.cron。项目 hooks/assets 按 project_files 复制到角色产物根目录，命令相对对应安装目录执行。

公共适配器不包含业务镜像构建脚本。构建、部署、证书等资源放在项目 hooks/assets，并通过 workflow.verify 引用。完整输入输出及安装边界见 `docs/adapters.md`。
