# alpha-research-engine — Agent Rules

本仓库是 Alpha 系列的研究平台：数据采集、特征、仿真、回测、钱包链上行为分析等研究工作。原为 alpha-engine monorepo 的 `research/` 目录，已独立拆出（git 历史保留）。本文件规则与仓库内 [`.cursor/rules/`](.cursor/rules/) 保持一致。

## 项目边界

- 本仓库是单一 Python uv workspace：`packages/` 放跨 app 共享的基建（core/storage/chains/datasources/protocols/metrics），`apps/` 放各研究方向的应用。
- 生产交易产品（alpha-lp、alpha-grid）在独立的 alpha-engine 仓库；本仓库不 import 其代码，生产产品也不得依赖本仓库运行时。
- 本仓库只做数据层/特征层/模型层：不持有生产私钥，不签名或广播真实交易，不直接修改生产数据库。
- 本地 Postgres 复用 alpha-engine 仓库 `products/alpha-lp/infra` 启动的实例，本仓库只使用其中的 `alpha_research` 库（见 `docker-compose.yml`）。

## 改代码前先认知模块与上下游

- 修改已有代码前，先读懂该模块的职责与真实行为，并列出上游调用方与下游依赖；评估行为/数据/兼容性影响后再动手。
- 改动范围以正确落地所必需为限：不漏改调用方、测试与文档，也不顺手扩大重构。未写出认知与影响不得改实现。

## 写代码前先规划

- 实现或修改业务逻辑、有状态流程、持久化之前，先在回复中写出数据流图、状态机、核心数据公式和数据表结构；不适用的项写「本次无」。
- 未写出规划不得动手改实现。改表时规划结果须同步 `SCHEMA.md`。
- 纯文案、注释、格式、重命名、与数据无关的机械修复可跳过。

## 方案选择：优先最佳，折中须明示

- 优先选择当前问题下在正确性、可维护性、扩展性和长期目标上最合适的方案，不因历史实现、改动面或沉没成本默认折中。
- 可以增量落地，但增量路径必须朝向最佳终态，不得把临时方案固化。
- 一旦采用折中方案，回复中必须说明具体约束、最佳终态，并比较两者的正确性、复杂度、风险和维护成本。

## 中文注释

- 新增模块、公开函数/类、重要私有逻辑、业务分支和风控规则使用中文说明，解释为什么以及业务含义。
- 枚举或联合取值的集合及每个成员都要说明中文语义。
- dataclass、TypedDict、pydantic/ORM 模型的每个字段都要说明单位、范围和空值含义。
- 公开函数和重要私有函数说明参数、返回值及必要的业务异常；简单赋值和纯透传不堆砌注释。

## 抽象与业务边界

- 业务代码只保留研究语义和领域不变量；校验、缺省、转换和序列化优先由现有类型/schema/框架声明。
- 抽象重复的技术逻辑，不把清晰的业务决策拆成伪通用层。
- HTTP、CLI、RPC、数据库等适配层保持薄，领域模型保持无 I/O 或显式端口依赖；`packages/storage` 是唯一直接碰 DB 的地方。
- 遵循已有技术栈，不为同一能力另起一套实现。

## 临时代码隔离

- 历史数据修补、字段改写、一次性灌数和临时探测代码必须放在 `scripts/oneoff/` 或等价隔离目录。
- 临时代码不得被应用启动、定时任务、主 CLI 或默认测试路径 import；必须有明确执行方式、删除条件和期限。
- 正式 Schema migration 放 `packages/storage/src/alpha_storage/migrations/`（Alembic），不与 oneoff 混放。

## 表结构文档

- 仓库根 `SCHEMA.md` 是表结构唯一文档真相。
- 新增、删除、重命名表或字段，以及类型、可空、默认值、约束、索引和业务语义变化，必须在同一改动中同步文档。
- 文档只保留当前完整结构，不维护历史迁移过程；每张表列出完整字段、约束和索引。

## Utils 与共享代码

- 无领域决策的规范化、时间、格式化和通用常量放在 `packages/core` 或所属包内独立的 utils 模块；utils 不得反向依赖业务层。
- 强业务语义逻辑留在所属 app，禁止为了目录整齐制造第二业务层。

## 结构化日志

- 关键业务节点（同步、任务起止、数据回填）打 `info`/`warning`；出错处必须落盘，禁止空 `except`。
- 链上 RPC、外部 HTTP、429/限流/配额失败必须记录，并区分短时限速和计划额度耗尽。
- 用标准库 `logging`，不用 `print` 当运行日志；不打密钥与鉴权头；不在每个成功只读 RPC 上刷屏。

## 安全与生成文件

- 密钥、真实 `.env`、数据库、Parquet、日志、缓存和构建产物不得提交。

## 规则索引

- [改代码前先认知](.cursor/rules/understand-before-change.mdc)
- [写代码前先规划](.cursor/rules/plan-before-code.mdc)
- [优先最佳方案](.cursor/rules/prefer-best-solution.mdc)
- [中文注释](.cursor/rules/chinese-comments.mdc)
- [抽象与少样板](.cursor/rules/prefer-abstraction.mdc)
- [临时代码隔离](.cursor/rules/oneoff-migrations.mdc)
- [表结构文档](.cursor/rules/schema-docs.mdc)
- [Utils 与业务解耦](.cursor/rules/utils-package.mdc)
- [结构化日志](.cursor/rules/structured-logging.mdc)
