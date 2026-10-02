"""alpha_storage：research 工作区唯一直接访问数据库的包。

apps 里不允许出现裸 SQL；加索引、改分区策略、换存储引擎只需要动这一个包。
表结构的唯一文档真相是 research/SCHEMA.md，改表必须同一改动内同步。
"""
