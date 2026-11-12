# 系统实现参考与恢复清单

此目录恢复此前开源版遗漏的实现。它们是方法的真实工程来源，**不构成开箱即用的完整生产运行器**。禁止把缺失依赖用空数据或伪回执补成通过。

| 文件 | 作用 | 外部依赖 |
|---|---|---|
| distill.py | 全本索引、有界阅读、逐章/单元schema、验证、恢复与整书合成 | 自有原文、来源目录、模型调用和项目运行目录；作品列表已清空 |
| context_compilers.py | 原分层检索及完整原子记录编译函数摘录 | registry/task/project/policy/权限检查等原项目服务；不能单独执行 |
| autonomous_production.py | 单元总控、章生成、冷读/事实、分支记忆与断点 | 配置、角色库、事实与适配模块；保留历史路径结构以便审查 |
| autonomous_quality.py / natural_expression.py | 分组表达检查、确认、修复与证据账 | 评估器、配置、校准样例与父运行器 |
| production_efficiency.py | 任务累计预算、精确复用、文件锁与下一动作 | 项目运行目录与调用回执 |
| reader_known_state.py | 读者已知状态的证据绑定、视图提取与失效处理 | 使用者的分支状态文件和正文证据 |
| unit_story_selection.py | 从完整单元选择章范围，保持上游因果 | 相关规划、世界对齐和方法库 |
| appeal.py | 单书完整窗口选择、预算/缓存、appeal与treatment校验 | 合法参照目录及其源文件 |
| dialogue_retrieval_production.py | 独立ABC召回后完整窗口适配审计 | 语料/标注/配置/schema/模型客户端 |
| dialogue_fewshot_gate.py / fewshot_reference_guarantee.py | 对话召回、准入、降级与来源校验 | 使用者自己的合法语料资产 |

`source-map.json`记录来源文件SHA及公开处理方式。未打包：小说原文、私人正文/角色/Canon、真实任务日志、原有生产配置与凭据。公开版的可独立工具位于`tools/`；本目录的编译检查只证明语法，不代表缺失依赖已补齐。
