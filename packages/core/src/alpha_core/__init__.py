"""alpha_core：research 工作区的领域模型层。

不依赖任何外部服务（数据库、RPC、HTTP），保证领域逻辑可以脱离基础设施单独测试。
其他 packages/apps 只应该从这里导入领域对象，不应该重复定义。
"""
