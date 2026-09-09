# 交付验证

最终安装包：

- [DSH 插件](../releases/dsh-vane-0.1.0.tgz)
- [Python wheel](../releases/dsh_vane_runtime-0.1.0-py3-none-any.whl)
- [安装与配置说明](../README.md)
- [机器可读回执、版本与 SHA-256](verification.json)

Node 17 项通过；Python 9 项通过、1 项真实 PostgreSQL 测试跳过。构建、类型检查、锁定安装、npm 打包清单、实际 DSH CLI 安装与 Prompt/Skill 注入、黄金产物互操作、隔离安装 wheel 后的真实 Vane 计算均通过。最终 npm 分发包与宿主测试实际安装的 tarball SHA-256 相同。

[宿主测试](host/receipt.json)使用确定性模型替身。[真实视觉抽取](vision/receipt.json)读出 A=15、B=25。两者均不计入自主研究成功项。

[真实自主研究](research-adapted/receipt.json)未通过：Qwen2.5-VL-3B-Instruct 仅输出计划/拟调用文本，未实际调用工具；完整模型原文和轨迹保留在 research 与 research-adapted 目录。

在线 PostgreSQL 尚缺 DSH_VANE_TEST_PG_DSN；在线 WeKnora 尚缺 DSH_VANE_TEST_WEKNORA_URL/KEY/KB/KNOWLEDGE，见[回执](weknora/receipt.json)。未配置的服务测试没有计入通过项。WeKnora 原始文件下载适配尚未实现，当前支持索引正文与授权共享产物；原件可通过文件入口加载。
