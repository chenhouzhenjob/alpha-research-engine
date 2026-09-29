"""alpha_protocols.config：链画像和协议实例配置的加载与校验。

配置文件（`chains/*.yaml`、`instances/*.yaml`）是协议知识的唯一真相，走代码评审；
这里负责把它们读成类型化的对象，所有错误在加载时就报出来，不带到解码阶段。
"""
