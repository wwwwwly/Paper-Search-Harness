# 配置指南

所有命令都从项目根目录运行。默认配置随 Python 包分发；本地覆盖文件是一份**完整配置**，不与默认配置做隐式合并。

## 本地配置与凭据

复制 `backend/paper_search/config/default.yaml` 为 `config.local.yaml`。

```bash
# macOS / Linux
cp backend/paper_search/config/default.yaml config.local.yaml
export PAPER_SEARCH_CONFIG="config.local.yaml"
export PAPER_SEARCH_MODEL_API_KEY="your-model-api-key"
```

```powershell
# Windows PowerShell
Copy-Item backend/paper_search/config/default.yaml config.local.yaml
$env:PAPER_SEARCH_CONFIG = "config.local.yaml"
$env:PAPER_SEARCH_MODEL_API_KEY = "your-model-api-key"
```

环境变量优先于 YAML 中对应的 `api_key`。`PAPER_SEARCH_MODEL_API_KEY` 会覆盖所有模型配置的密钥；多服务商使用不同凭据时，在已忽略的本地配置中分别填写，并取消该全局环境变量。示例文件中的空字符串和 `your-…` 都是占位符。

`PAPER_SEARCH_CONFIG` 未设置时使用包内默认配置。数据库和 `data_dir` 的相对路径以启动进程的工作目录为基准。修改配置后重启后端。

## 模型

`models` 定义服务连接，`efforts` 将 Quick / Deep 映射到模型及预算。默认示例使用 DashScope；更换服务商时请一并检查：

| 字段 | 含义 |
| --- | --- |
| `model` / `base_url` | 模型标识与兼容 API 的基础地址。 |
| `api_key` | 凭据，推荐通过环境变量提供。 |
| `context_window` | 模型的上下文容量。 |
| `max_output_tokens_supported` | 服务商允许的输出上限，用于输出容量处理。 |
| `supported_reasoning_efforts` | 配置允许的推理强度。 |
| `capabilities_url` | 可选的能力目录地址；当前解析器适配 DashScope，其他服务商可设为 `null`。 |
| `force_max_output_tokens` | 服务商需要显式输出上限时启用。 |
| `extra_body` | 服务商要求的附加请求字段。 |

适配器使用 Chat Completions 风格的流式接口、工具调用和 usage。不同兼容服务的字段支持可能不同；默认推理参数为 Quick `low`、Deep `high`，需要与你选择的模型一致。`context_output_headroom` 为上下文预留输出空间，不是每次生成的固定长度。

价格字段只用于用量展示，不参与运行准入。默认值是配置示例，请按实际服务价格调整。

## Quick 与 Deep

| 参数 | Quick | Deep |
| --- | ---: | ---: |
| 目标时间 | 90 秒 | 600 秒 |
| 硬截止 | 180 秒 | 900 秒 |
| 收尾预留 | 30 秒 | 90 秒 |
| token 软目标 | 64,000 | 256,000 |
| Worker 并发上限 | 0 | 3 |

token 软目标触发缩小范围的提示，不是硬性费用或 token 上限。时间截止会限制继续探索，收尾阶段使用已有证据提交结果。新用户输入或显式扩展预算可以开启新的执行预算段；自动重试不会重置截止时间。

## 向量检索

默认 `embedding.enabled: false`，可直接使用关键词检索。需要语义检索时，在本地配置中启用 embedding，并确认模型与维度受服务商支持：

```yaml
embedding:
  enabled: true
  model: qwen3.7-text-embedding
  base_url: https://dashscope.aliyuncs.com/compatible-mode/v1
  api_key: "" # 使用 PAPER_SEARCH_EMBEDDING_API_KEY
  dimensions: 256
```

设置 `PAPER_SEARCH_EMBEDDING_API_KEY` 后重启后端。已有文档和记忆可以在工作台重新索引。向量请求会调用外部服务，文档片段或查询会发送给相应的 embedding 服务商。

## 网页检索与 MCP

默认只启用 arXiv；网页发现可通过 MCP 扩展。下面是 Exa HTTP MCP 的配置示例，替换本地配置中的 `mcp: []`：

```yaml
mcp:
  - name: exa
    transport: http
    url: https://mcp.exa.ai/mcp
    tools:
      - web_search_exa
      - web_fetch_exa
    read_only_tools:
      - web_search_exa
      - web_fetch_exa
    timeout_seconds: 60
```

工具名称、鉴权与配额以 MCP 服务实际提供的接口为准。需要密钥时，将鉴权 URL 或 `headers` 写入 `config.local.yaml`，不要写入默认配置。`read_only_tools` 决定哪些外部工具被视为可安全重放，应只列出确认没有写入副作用的工具。

也支持 `stdio` 连接，使用 `command`、`args` 和 `env` 指定本地 MCP 服务。连接状态和已启用工具可在工作台的“设置与记忆”中查看。

## 本地存储与运行

`data/research.db` 保存会话、任务、证据和事件；`data/index.db` 保存派生检索索引；`data/artifacts/` 保存原始页面、PDF、工具结果与模型输入输出。文件可能包含研究内容和服务响应，整个目录默认被 Git 忽略。

后端按单实例运行，启动锁位于数据目录。请保持一个后端进程，不要为同一数据目录启用多个 Uvicorn Worker。前端开发服务把 `/api` 转发至 `127.0.0.1:8000`；修改后端端口时同步修改 `frontend/vite.config.ts`。

常见启动问题：

- **缺少模型凭据**：工作台可以正常打开，模型调用会提示配置凭据。在启动后端的同一终端设置环境变量，或填写本地 YAML 后重启。
- **词表下载失败**：首次使用 tiktoken 需要下载词表。离线环境可通过 `TIKTOKEN_CACHE_DIR` 指向已准备好的缓存目录。
- **MCP 不可用**：检查服务地址、工具列表和鉴权；未配置 MCP 时仍可使用 arXiv。
- **PDF 没有可读文字**：当前解析器不执行 OCR，扫描件需要先转换为包含文本层的 PDF。
