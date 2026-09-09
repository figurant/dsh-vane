# dsh-vane

DSH 原生插件 + 任务专属常驻 Python/Vane 运行时。DSH 负责规划、选择工具和撰写结论；Vane 保留表、UDF、代码记录、证据和产物，供后续模型轮次继续计算。

依据[设计文档](https://ocnq8kyde63e.feishu.cn/wiki/DpyXw5jQLi3psmkPqbfcUNGSnyx) revision 11 实现。已核验 DSH 源码 `49a606bc5b5934603f22a26957a07dc799ab0291`，实际安装并测试的 npm 宿主为 **0.1.2-rc.1**，Vane 为 **vane-ai 0.1.0**。使用公开 npm 包，不导入其他仓库的源码，不依赖 weknora-vane 或 vane-docreader。

## 安装与启动

需要 Linux/POSIX、Node.js 22+、Python 3.11/3.12、uv。当前验证环境为 Linux、Node 24、Python 3.11。Vane UDF 需要本地进程、套接字和共享内存权限；受限沙箱中可能不可用。

```bash
cd /path/to/dsh-vane
npm ci
npm run build
uv sync --locked
uv run python scripts/probe-vane.py
```

Python 安装独立放在项目 `.venv`。npm 安装/打包不下载模型权重。锁定依赖包含 Vane、Arrow、PDF/图像读取与 PostgreSQL 客户端；包依赖在运行时启动时核验，上传文件不能触发依赖安装。

先用专用 profile 验证（脚本自动创建独立 `DSH_HOME`，安装实际 npm tarball）：

```bash
npm run test:host
```

本机 CLI `--help` 核验的安装语法是 `dsh plugin --profile <name> add <package>`。使用本项目安装的宿主时：

```bash
export DSH_VANE_PYTHON="$PWD/.venv/bin/python"
export DSH_VANE_WORKSPACES="$PWD/.runtime/workspaces"
export DSH_VANE_FILE_ROOTS="$PWD/examples/materials"
node_modules/.bin/dsh plugin --profile headless add "$PWD"
node_modules/.bin/dsh --profile headless "分析 @examples/materials/history.csv"
```

最后一条命令需要该 profile 已配置推理模型。默认包 patch 只插入 `id: vane / name: dsh-vane`。要加入现有 web profile，使用相同 `plugin --profile web add` 命令，并把 [profile.patch.example.yml](configs/profile.patch.example.yml) 中的 `vane` 行合并进已有 patch 列表。不要覆盖整个 profile；本项目未修改用户现有 web、LLM、dsh-weknora 配置或 UI。

发布/离线分发：`npm pack` 包含编译代码、Prompt/Skill、Python 源码、contracts、样例包与 `uv.lock`；解包后运行 `uv sync --locked`，或安装 `uv build` 生成的配套 wheel，并把 `pythonExecutable` 指向该环境。wheel 内含协议 Schema 和依赖锁；业务包与宿主配置仍由 npm 包提供。

## 配置

完整模板见 [configs/example.json](configs/example.json)。模板中的路径、知识库和 scope 占位符必须替换；未配置的外部能力不会出现在可用来源目录。

| 字段 | 说明 |
| --- | --- |
| `pythonExecutable` | 绝对路径，默认本 npm 包根目录 `.venv/bin/python` |
| `runtimeModule` | 默认 `dsh_vane_runtime`；仅宿主配置可设置 |
| `workspaceRoot` | 默认 `~/.local/share/dsh-vane/workspaces` |
| `allowedFileRoots` | 允许加载的绝对目录，默认宿主 cwd；符号链接解析后复核 |
| `enabledPackages` | `{id,version,path,digest}` 白名单；省略时启用内置包；`[]` 禁用包 |
| `artifactStores` | `{alias,root,scope}`，本机共享的不可变产物目录 |
| `weknoraSources` | `{alias,baseURL,apiKeyEnv,knowledgeBaseIds}`；显式知识库白名单 |
| `databases` | `{alias,kind:'postgresql',dsnEnv}`；只实现 PostgreSQL |
| `models` | `{alias,baseURL,model,apiKeyEnv?,temperature?,maxTokens?,revision?}`；包内视觉/抽取模型，与 DSH 推理模型独立 |
| `hostVersion` | 当前只接受 `0.1.2-rc.1`；真实 registry/assemble 测试验证接口 |
| `limits` | 默认 4 个活动工作区、每区排队 32 个操作、单操作 120 秒、空闲 30 分钟、取消宽限 1.5 秒、Vane 内存 1 GiB、磁盘 1 GiB、10 万结果行、100 MiB 文件、64 KiB 响应、1 MiB IPC 帧 |

密钥只通过环境变量引用，不进入工具 Schema、能力目录或模型日志。Vane/任务 Python 是受信任本机代码，具有该进程的环境和文件权限；文件入口检查、Vane 内存设置和超时并非强隔离沙箱，也不限制任意 Python 的全部内存分配。强隔离需另配容器/操作系统策略。

## 六个工具

工具结果统一为 `{ok,workspace_id,operation_id?,status,data?,error?}`，错误包含 `code/message/retryable`。宿主会二次校验 Schema、模式字段和数值边界。ID 只在所属会话目录中有效。

| 工具 | 用途 |
| --- | --- |
| `vane_open` | `{new_task?,package_ids?}`。默认复用本会话当前任务；新任务不清除旧任务 |
| `vane_load` | `{workspace_id,source,table_name?}`。文件、WeKnora、产物或只读数据库 |
| `vane_describe` | `{workspace_id,target:'workspace'\|'table'\|'functions',name?}`。目录、Schema、有限样本、来源、UDF/流程签名；仅 table 需要 name |
| `vane_execute` | SQL、Python 或已启用包的 pipeline；只接受所选模式的字段 |
| `vane_read` | `{workspace_id,result_id,offset?,limit?}`。默认 20 行、上限 100；返回 next_offset/truncated/full_result |
| `vane_control` | status/cancel 必须给 operation_id；checkpoint 必须显式给 tables；restore 必须给 checkpoint_id；close 关闭任务 |

`wait_ms` 默认 150 ms，最大 2000 ms（配置可提高到 5000 ms）。查询、加载、描述等操作在每个工作区内排队；长操作先返回 `queued/running + operation_id`，用 control 查询。不同工作区并行。表统计未计算时 `row_count: null`，不冒充 0。结果大于响应预算时给分页/完整本地 JSONL 引用，单个超大单元格也不强塞进聊天。

SQL 示例：

```json
{"workspace_id":"<open返回的ID>","mode":"sql","sql":"SELECT item, sum(value) total FROM historical GROUP BY item"}
```

`bindings` 是 Vane 的值参数绑定，不用于表名。命名表/视图继续存在，单次结果另行物化到 `_vane_results`，分页不重跑原 SQL。用户 SQL 的事务和数据库 attach/detach 由宿主管理。

Python 示例：

```python
def adjusted(value):
    return value * 1.05

ctx.register_udf('adjusted_v1', adjusted, ['DOUBLE'], 'DOUBLE')
ctx.connection.execute('CREATE TABLE adjusted_rows AS SELECT item, adjusted_v1(value) value FROM historical')
ctx.publish('adjusted_rows')
```

`ctx.connection/register_udf/publish` 是本项目接口。注册最终调用 `vane.func` / `vane.attach_function`；UDF 名称不能覆盖已有版本。

## 文件、知识与数据库入口

DSH `@file` 只插入路径文本。用户把文件放进允许目录后，Agent 必须显式调用 `vane_load`；传入 path 不带 `@`。相对路径以可信的 `exec.agent.session.header.cwd` 解析，缺失时只能使用允许的绝对路径。原件复制到任务输入区并计算 SHA-256。

```json
{"workspace_id":"<id>","source":{"kind":"file","path":"history.csv"},"table_name":"historical"}
```

CSV/Parquet 建表；PDF/图片先登记 asset，再由 Agent 选择包或 UDF，不会把“文件存在”报告成“内容已读取”。样例材料在 [examples/materials](examples/materials)，可用 `uv run python scripts/make-example.py` 重新生成。

WeKnora：dsh-weknora 负责检索发现 knowledge ID；本插件以配置身份独立回读公开 `GET /api/v1/knowledge/:id` 和 `GET /api/v1/chunks/:id`，核验知识库、分页、分块顺序、完整性与权限。description 保持为摘要字段，不作为正文。指定 chunk_ids 时仅承诺这些分块。

```json
{"workspace_id":"<id>","source":{"kind":"weknora","source_alias":"knowledge","knowledge_id":"<knowledge-id>"}}
```

正文的 `VANE_ARTIFACT:<uuid>` 只用于定位，不授权。共享产物须绑定同一可读知识引用，重新读取并核对标记、store scope、路径、manifest 和 SHA。没有标记、目录不可达、校验失败会返回不同的 artifact_status；仍可读取已授权正文，不声称有 facts 表。`store_alias:'workspace'` 只读取本会话拥有的产物。

当前入口加载索引正文及共享结构化产物，未实现原始 WeKnora PDF 的下载适配；需要原件时先通过已授权的公开下载途径保存到允许目录，再 `load(file)`，不会读取 WeKnora 内部存储或自动回写知识库。

PostgreSQL：`source={kind:'database',source_alias,query,bindings?}`，绑定语法是 psycopg 的 `%s`。使用只读事务、服务端游标、statement_timeout、行数和字节限额，然后物化到任务表。生产环境还应配置只读数据库账号。其他数据库/数据湖不在首版能力范围。

## 可移植处理包与产物

[document_observations](packages/document_observations) 是通用运营记录样例，不含金融公司或预测逻辑。CSV 使用 item/value/category，文本 PDF 示例使用 `item,value[,category]` 行；图片和扫描 PDF 要选择配置的 model_alias。参数、抽取 Prompt 和业务转换在包内，连接器不猜测口径。未识别的正文保留为证据，不伪造数值。

```json
{"workspace_id":"<id>","mode":"pipeline","package_id":"document_observations","pipeline":"ingest","asset_ids":["<load返回的asset_id>"],"params":{"multiplier":1}}
```

每次新配方使用独立 `run_<uuid>` schema，建立 input_files 和 run_params，按声明执行 SQL，并返回完整表名。旧表和输入保留。模型补抽通过新 prompt/response_schema 参数形成新版本。包示例通过 `DSH_VANE_MODELS` 读取宿主声明的模型别名，秘密仍由 apiKeyEnv 引用。

协议文件：

- [package-v1.schema.json](contracts/package-v1.schema.json)：`vane-package/v1`；包的文件清单、UDF 声明、流程与输出。
- [artifact-v1.schema.json](contracts/artifact-v1.schema.json)：`vane-artifact/v1`；支持扩展字段，未知主版本/格式和缺失必填项明确失败。
- `uv run python scripts/package-digest.py <package-path>`：按规范 POSIX 路径排序、逐文件 SHA、UTF-8 规范 JSON 计算摘要。校验后的固定版本与摘要写入 enabledPackages。包之间隔离 Python 导入命名空间。

documents/evidence/images 使用协议规定的固定字段，facts 自定。PDF 页码从 1 开始，CSV 证据行号包含表头，图像 bbox 为归一化坐标，未知位置是 `{}`。图像必须是工作区内可解码的真实文件，发布时复制字节并保留 original_ref 映射。

成功产物先在 `.pending-*` 下构建与校验，再原子发布 UUID 目录；含 manifest、Parquet/JSONL、原件副本及 SHA。相同成功配方重试返回原 artifact_id；输入、包/代码、参数、宿主配置、版本、锁、模型及 Prompt/Schema 影响配方摘要。模型别名和随机性不保证位级复现。recipe 使用受限规模的输入表快照，超限时应先做显式 SQL 聚合。

黄金夹具见 [tests/fixtures/golden/receipt.json](tests/fixtures/golden/receipt.json)。`npm run test:interop` 从真实 `item,value\nA,10\nB,20\n` 字节运行包，然后以独立 JSON Schema + hashlib + PyArrow 读取器验证两行、合计 30、证据行 2/3。消费其他项目的同协议黄金目录：

```bash
uv run python scripts/interop.py --consume /path/to/artifact-uuid
```

## 生命周期与恢复

宿主以 `exec.agent.id` 绑定所有权，不接受模型提供 session_id。每个工作区有常驻 Python 进程及 Vane 连接，stdin 请求与 fd 3 JSON 响应分离，stdout/stderr 只保留有界内存日志。Vane 自身还会启动本地 UDF worker，不能把它描述为与 Node 完全同进程。

取消先用真实核验过的 `connection.interrupt()`；卡死 Python/UDF 超过宽限期，回收整个进程组，工作区标 lost。控制通道独立于工作线程，查询和取消无需等待重操作完成；迟到响应不能改写已取消终态。SQL 事务不撤销外部模型请求或任意 Python 文件副作用。

```json
{"workspace_id":"<id>","action":"checkpoint","tables":["main.historical","run_<id>.facts"]}
```

检查点排在已有工作之后，保存显式物化表、原件、序列化任务 UDF、包摘要和执行记录，并有独立 manifest SHA。`restore` 新建工作区，核验所有权、版本、包与文件 SHA 后重建表和 UDF，原实例保持独立。没有物化的 Relation、任意 Python globals、闭包里的不可序列化连接等不会假装恢复；不可序列化 UDF 会明确报错。检查点包含受信任代码，只能读本会话检查点，不能从知识正文反序列化代码。

普通回答结束保持实例。close、空闲回收和插件卸载关闭连接、终止进程组；活动/排队工作不被空闲计时误杀。关闭时已经完成的结果文件和数据库保留，未做检查点的内存对象无法在新进程恢复。

## Prompt、Skill 与验证

[prompts/data-agent.md](prompts/data-agent.md) 和 [skills/data-analysis/SKILL.md](skills/data-analysis/SKILL.md) 都由 `ctx.systemPrompt.section` 注入，使用独立名称、普通排序；未设置 `complete=true`。为避免依赖用户部署的 Skill provider，当前通过同一 Prompt 贡献机制直接加载运行时 Skill，不要求用户手工粘贴或把仓库 AGENTS.md 当 Skill。

```bash
npm run build
npm run typecheck
npm test
npm pack --dry-run
uv sync --locked
uv run pytest -q
npm run test:host
npm run test:interop
npm run test:research -- --output receipts/research-new
uv run python scripts/verify-vision.py
node scripts/verify-weknora.mjs
```

宿主回执见 [receipts/host/receipt.json](receipts/host/receipt.json)，包含真实 tarball 安装、CLI 版本、实际模型输入和工具运行。它使用确定性模型替身，只验证宿主连接，不冒充自主研究。

真实研究默认连接本机 `http://127.0.0.1:8001/v1` 的 Qwen2.5-VL-3B-Instruct；可通过 `DSH_VANE_RESEARCH_URL/MODEL/KEY_ENV/MAX_STEPS/OUTPUT` 改配置。脚本启动实际 DSH AgentLoop，给目标和材料，再追加文件追问；不预排工具序列。完整原文和轨迹保存在输出目录。当前两次真实模型实验均未通过自主分析验收：模型只写计划/拟调用文本，没有实际调用工具，不能据此宣称研究成功。详见 [receipts/research-adapted/receipt.json](receipts/research-adapted/receipt.json) 和 [原始输出](receipts/research-adapted/conclusions.md)。测试适配层只处理该演示服务不支持流式、合并消息角色的兼容问题，没有修改 DSH 核心或模型服务。

真实 PostgreSQL 测试需要 `DSH_VANE_TEST_PG_DSN`。真实 WeKnora 只读验收需要 `DSH_VANE_TEST_WEKNORA_URL/KEY/KB/KNOWLEDGE`，应指向专用测试知识库和文档；未配置时显式 blocked。单元测试使用公开 HTTP 模拟服务覆盖分页、截断、指定分块、摘要与正文区分、权限拒绝和撤权；模型替身测试与真实研究分开。未配置的真实外部服务不计入通过项。

当前确定性测试为 **17 项 Node 测试通过，9 项 Python 测试通过，1 项真实 PostgreSQL 测试跳过**。真实图像抽取已通过：[视觉回执](receipts/vision/receipt.json) 中 Qwen 从图片读出 A=15、B=25；这仅证明真实视觉抽取链路，不代表自主规划验收通过。构建、类型检查、锁定安装、宿主 tarball 安装和协议互操作也有独立回执。验证脚本属于开发/验收工具，使用项目开发依赖；生产插件只依赖自己的运行依赖与已安装的 Python 环境。
