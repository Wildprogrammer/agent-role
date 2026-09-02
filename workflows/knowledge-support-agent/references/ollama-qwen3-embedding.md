# Qwen3 Embedding 本地运行说明

本工作流仅连接用户自行管理的回环 Ollama，不启动服务、不修改配置，也不自动下载模型。增强检索固定使用 `qwen3-embedding:0.6b`；模型或服务不可用时自动使用全文检索，知识问答主流程仍可运行。

只读检查：

```powershell
ollama list
curl.exe http://127.0.0.1:11434/api/tags
```

如果用户明确要求验证混合检索，但本机尚无模型，应先说明官方下载来源、约 639 MB 下载量、Ollama 的实际存储位置和回滚方式，再取得大数据量下载确认。确认后才可运行：

```powershell
ollama pull qwen3-embedding:0.6b
```

回滚只删除该模型，不删除 LanceDB、Agent 配置或用户文档：

```powershell
ollama rm qwen3-embedding:0.6b
```
