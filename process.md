# PyWeKnora 复刻进展

更新时间：2026-10-18

## 目标

使用 Python 完整复刻 `/home/sharkyai/WeKnora`，复用其既有 Web UI，不重新构建 Vue。

## 仓库与运行约定

- 工作目录：`/home/PyWeKnora`
- 远程仓库：`origin git@github.com:shark8848/PyWeknora.git`（fetch/push 均已确认）
- Python：`/home/ikc-log-center/.venv/bin/python`
- 既有 UI：`static/web/`
- 当前分支：`master`，尚无 commit，全部项目文件仍为未跟踪状态
- 未擅自 commit 或创建分支

## 已完成

### 核心后端与 UI

- FastAPI 后端主体、SQLite 持久化、认证/租户/组织、知识库/文档/分块、检索、聊天、Agent、MCP、记忆、Wiki、嵌入渠道、系统管理等模块已落地。
- 复用既有 `static/web` 构建产物，保留 SPA 静态文件和 API 代理路由。
- Swagger 定义的路由已基本覆盖；当前实现另有少量兼容/扩展路由。

### 技能与文件访问

- 技能目录生命周期：列表、上传/注册、安装到 sandbox config、读取文件、已安装时阻止删除。
- 技能来源解析：ClawHub、`@owner/slug`、`skills-sh:`、GitHub/GitLab tree/blob/archive、skills.sh、SkillHub、注册表下载地址和子目录。
- Agent `read_file` 已覆盖会话工作区：有 sandbox config 时无需 skills 也注册；相对路径解析到 `/workspace`，绝对路径保留在沙箱内（含 `/tmp`），通过会话绑定的 Docker/Cube/E2B 执行 stat/读取，不触碰宿主文件。支持 `offset/limit/max_bytes`，默认 2000 行、单页最多 64 KiB、输出预算 24000 rune，并在分页结果中返回续读 offset；目录、符号链接、非普通文件和超过 8 MiB 的文件会被拒绝，二进制内容只返回元数据。`skill://` 资源复用同一分页/二进制路径。
- Agent `list_sandbox_files` 已接入无 shell 场景：有 sandbox config 且未注册 `shell_exec` 时自动提供；递归扫描普通文件、跳过目录/符号链接/特殊文件，默认 `/workspace/output`，相对路径锚定 `/workspace`，按路径排序并默认限制 200 条（最高 500），缺失根目录返回空列表，列目录失败返回明确错误。沙箱实现使用 NUL 分隔的 `find` 输出，能安全处理含空格/换行的路径。
- Agent `write_sandbox_file` / `edit_sandbox_file` 已对齐 Go：有 sandbox config 即注册；写文件支持 overwrite/append，相对路径锚定 `/workspace`，拒绝 `/workspace/input`、目录根、二进制和超过 8 MiB 的内容，append 只接受已存在的普通文件并校验累计大小；编辑限制 256 KiB、拒绝二进制，批量 `old_string` 均针对原始内容匹配，默认唯一、`replace_all` 全量、重叠整批拒绝，失败不写入。结果只返回路径/替换数/大小，不回显正文；上传采用 base64 分块、同目录临时文件和原子 `mv`。
- `/api/v1/skills` 返回启用且未删除的安装记录，支持 `sandbox_config_id`。
- 受保护文件代理覆盖 `/files`、知识库文件、会话消息文件和 embed 渠道文件；本地引用限制在租户文件目录内。
- 对象存储读取代理已接入：解析 `storage://<backend-id>/<provider>://...`，按租户查询 `storage_backends`（回退 legacy alias / 默认实例 / 租户 storage engine config / 环境变量），校验 provider、bucket 与路径租户段，再以签名 GET 流式返回。覆盖 S3、MinIO、COS、TOS、OSS、KS3、OBS，`resource://` 指向对象存储时同样适用。
- 对象存储请求复用 URL Guard 的 DNS 校验、固定 IP、重定向边界和 `ssrf.whitelist`；跨租户 backend id 查询不到时返回 404，不泄露实例是否存在。远程 4xx/5xx 统一按文件不存在处理。
- 知识上传已接入对象存储写入：按 KB `storage_backend_id`、legacy alias、租户默认实例或 legacy provider 解析目标；上传写入 `storage://<backend-id>/<provider>://<bucket>/<tenant>/<knowledge>/<uuid>.<ext>`，签名 PUT 覆盖 S3/MinIO/COS/TOS/OSS/KS3/OBS。
- 对象存储文档的解析、预览、单文件下载和批量下载均可通过同一引用回读；删除知识/KB/批量内容时清理对象，且有其他活跃文档共享同一 `file_path` 时跳过删除，避免 KB 复制共用引用被误删。
- KB copy worker 会把源文件复制到目标知识 ID 名下；对象存储通过服务端 CopyObject 生成目标知识前缀下的独立对象，本地存储写新的租户/知识目录。复制同时深拷贝 chunk `image_info` 中引用的图片到目标知识自有前缀，按源 URL 去重，保留未知字段并同步改写匹配的 `original_url`；所有 chunk 处理完后统一重写正文与 `source_content` 的图片 URL，覆盖父文本 chunk 引用子图片 chunk 对象的情况。新 chunk 的 `pre/next/parent_chunk_id` 会按源→目标 chunk ID 重映射。复制中断或插入失败时会 best-effort 清理本次新建文件。复制共享引用保护仍保留，作为异常中断和旧数据的兜底。
- `POST /knowledge-bases/{id}/duplicate` 已对齐上游 settings-only 语义：返回 201 与 `source_id/target_id/message/knowledge_base`，只复制 KB 设置，清空生成画像、临时/置顶状态，不复制文档、chunks、FAQ、索引、分享或置顶。
- FAQ 知识库 `/knowledge-bases/copy` 会复制 `py_faq_entries`：按规范化标准问题幂等新增/更新，复制同名标签并建立源 `seq_id` → 目标 `seq_id` 映射，保留答案、相似问题、启用/推荐状态；重复执行不会产生重复条目。
- `/api/v1/files/presigned` 已实现 GET/HEAD：HMAC-SHA256 覆盖 `file_path + tenant_id + expires`，密钥读取 `SYSTEM_SIGNING_KEY`/`SYSTEM_AES_KEY`，支持无 Bearer 鉴权的 IM 取图；HEAD 在返回 Content-Type/Content-Length 后关闭对象流，不读取正文。

### Wiki、FAQ、标签

- Wiki 统计修正：`pending_tasks` 统计 `wiki:ingest` / `knowledge_base` 待处理任务，`is_active` 反映队列活动。
- 新增自适应分块预览 `app/services/chunker_adaptive.py`，支持：
  - 文档画像与语言检测
  - `heading` / `heuristic` / `legacy` 分层选择与失败回退
  - `auto` 策略、父块/子块、protected spans、overlap
  - 64 KiB 输入上限、500 块响应截断、`truncated_to`、平均/最小/最大/标准差统计
  - 源文本 rune 偏移与 chunk 内容一致性
- `GET/POST /api/v1/chunker/preview` 均返回真实预览；POST 要求认证。
- FAQ 导入进度改为读取 `py_task_results`（`kind='faq_import'`），未知任务返回 404。
- FAQ 导入结果 `display_status` 可持久化为 `open/close`。
- `POST /knowledge-bases/{id}/faq/entries` 已改为异步任务：先写入 `py_task_results` 的 `pending` 记录，后台完成格式/重复/标签校验、按 50 条分批写入并更新 `processing/completed/failed` 进度。
- FAQ 导入支持 `dry_run`（只校验不写入）、`replace` 模式、批次内与知识库内重复问题检测；单知识库同时只允许一个 pending/processing 导入任务。worker 已接入 SQLite 跨实例认领/租约、启动恢复和重试：正式导入 `max_retry=5`，dry-run 为 3。
- FAQ worker 会遵守管理员取消：批次前后检查任务状态，进度更新仅允许 pending/processing，取消后不会覆盖终态或继续写入。
- `POST /knowledge/tags` 不再 no-op，与 `PUT /knowledge/tags` 共用真实批量标签更新逻辑。

### 运行时队列管理

- `GET /system/admin/runtime/queues` 不再返回 unavailable，改为从 `py_task_results` 统计 pending/retry/processing/completed/failed/cancelled 任务并投影为 `database` 队列。
- `GET /system/admin/runtime/queues/{queue}/tasks` 支持 pending/active/scheduled/retry/archived/completed 状态过滤、游标分页和任务详情；retry 任务返回 `next_process_at`，并允许 cancel/run_now。
- 任务动作支持取消 pending/processing、删除非运行任务，以及清理 failed/cancelled 归档；归档的 `faq_import` 支持 `run_now`，会回到 pending 且不重置重试计数，并重新调度 worker。
- 当前是单 SQLite 任务表投影，已通过原子认领、300 秒租约和每 60 秒续租支持跨进程 worker 互斥；FAQ 正式导入/dry-run 分别有 5/3 次重试预算，复制/移动/重解析有 3 次预算，退避为 10/40/90 秒，并投影 pending/retry 与真实 `retried/max_retry`。仍不等价于 Go 的 Redis/asynq 多队列、scheduled 调度与权重语义。

### 向量库注册与 CRUD

- `GET /api/v1/vector-stores/types` 已切换到 `app/services/vector_store_catalog.py`，返回上游使用的 `type/display_name/connection_fields/index_fields`。
- 注册目录覆盖 Elasticsearch、Qdrant、Milvus、Tencent VectorDB、Weaviate、Doris、OpenSearch；SQLite/PostgreSQL 为 env-only 存储，不进入可新增注册列表。
- 字段元数据包含类型、必填、敏感标记、默认值和描述；OpenSearch 额外包含 min/max、immutable 和 knn_engine enum。Tencent replica 默认值读取 `TENCENT_VECTORDB_REPLICA_NUMBER`。
- 列表先返回 `RETRIEVE_DRIVER` 派生的脱敏只读 env store，再返回当前租户的 DB store；`GET /vector-stores/{id}` 和 `POST /vector-stores/{id}/test` 支持 `__env_*` ID，测试使用未脱敏配置。
- 创建返回 201，按上游顺序校验名称、可注册引擎、连接必填字段、SSRF、索引名称/数值边界、同租户 endpoint+index 重复（含 env store）和连通性；连接失败返回 400，成功后把检测到的版本写入 `connection_config`。
- 更新仅允许提交 `name`，engine/connection/index 不可变；env store 的更新/删除返回 400 `environment-configured vector stores cannot be modified via API`。删除仍被 KB 绑定时返回 400，成功响应仅含 `success`。
- `POST /vector-stores/test` 只接受可注册引擎，先做必填/SSRF 校验；校验或连通性失败均返回 HTTP 200 + `success=false`。Python 实际检索运行时仍只接 SQLite，外部向量库尚未接入。

### 网络搜索 Provider

- `GET /api/v1/web-search-providers/types` 已切换到 `app/services/web_search_catalog.py`，按上游顺序返回 14 个 provider：brave、serply、duckduckgo、bing、google、tavily、ollama、searxng、baidu、keenable、metaso、zhipu、exa、bocha。
- 元数据覆盖前端实际读取的 `id/name/requires_api_key/supports_optional_api_key/requires_engine_id/requires_base_url/supports_proxy/description/docs_url/config_fields`；旧的简化 `provider/available` 字段已移除。
- Metaso 的 `scope`、Zhipu 的 `search_engine/content_size`、Exa 的 `include_text`、Bocha 的 `freshness/summary` 均通过 `extra_config` 接入运行时请求。
- `app/services/web_search.py` 已补齐 Serply、Baidu、Keenable、Metaso、Zhipu、Exa、Bocha 适配器，14 个已注册 provider 均可执行统一 `search()`；适配器按上游契约处理鉴权头、请求体、默认值、查询截断和响应字段映射。
- 创建/更新 provider 已接入保存时校验：provider 类型、Brave/Serply/Bing/Google/Tavily/Ollama/Baidu/Exa/Metaso/Zhipu/Bocha 的必填密钥、Google engine ID、Metaso scope、Zhipu engine/content_size、Bocha freshness、SearXNG base URL 和可选 proxy URL。
- 网络搜索 `_http` 已应用 `proxy_url`，所有请求先做 SSRF 校验，重定向目标也会重新校验；`ssrf.whitelist` 数据库设置优先，未设置时回退 `SSRF_WHITELIST`，并始终合并 `SSRF_WHITELIST_EXTRA`，均支持精确主机、`*.domain`、IP 和 CIDR。
- 系统设置写入 `ssrf.whitelist` 时接受 string list 或逗号分隔字符串，校验 CIDR/通配符格式并存储为 JSON 数组；读取设置时返回数组。
- 无代理网络搜索请求改走 `url_guard.request_url`：每次重定向重新解析并校验，连接固定到已校验 IP；代理路径先校验代理与目标，再通过显式 `ProxyHandler` 发送。
- SearXNG 请求已对齐上游默认值：`language=all`、JSON Accept、`WeKnora/1.0` User-Agent、12 秒超时，并过滤缺少 title/url 的结果。
- 尚未用真实私有 SearXNG 实例和真实 HTTP 代理做端到端联调；当前由 mock、URL Guard 单测和 provider 保存校验覆盖。

### 模型并发限流

- `app/services/model_limiter.py` 只拦截后台 worker 的 chat/embedding 调用，交互请求不受影响。未配置 `REDIS_ADDR` 时使用进程内按模型 ID 的并发信号量；配置后自动切换到 Redis 分布式信号量。
- Redis 后端与 Go 实现对齐：ZSET 成员为请求 token、score 为 lease 到期时间，key 前缀 `weknora:modelsem:`，默认 30 秒 lease、200 毫秒轮询；Lua 原子清理过期租约、计数并在限额内准入。持槽期间每 10 秒续租 ZSET score 和 key TTL，进程崩溃后租约自动回收。
- Redis/网络错误与等待取消均 fail-open，不阻塞模型流量；`active` 是集群范围持槽数，`waiting` 仍是当前进程本地等待数。后端依赖 `redis>=5`，环境变量支持 `REDIS_ADDR`、`REDIS_DB`、`REDIS_USERNAME`、`REDIS_PASSWORD`、`REDIS_USE_TLS`、`REDIS_TLS_SERVER_NAME`、`REDIS_TLS_INSECURE_SKIP_VERIFY`。
- 模型自身正数 `max_concurrency` 优先，否则读取 `model.max_concurrency`，再回退 `WEKNORA_MODEL_MAX_CONCURRENCY`，默认 32；全局值为非正数时关闭限流。
- 知识复制、移动、重解析 worker 使用后台上下文；模型 HTTP 调用在 `finally` 中释放槽位，失败或异常不会泄漏占用。
- `GET /system/admin/runtime/queues` 的 `model_limiter_available` 改为真实可用状态，`models` 返回每个已观测模型的 active/waiting/limit。
- 新增 `tests/test_model_limiter_integration.py`（`WEKNORA_REDIS_INTEGRATION=1` 开启），使用临时本地 `redis-server` 验证跨实例限额、等待统计、心跳续租、过期租约回收、幂等释放、fail-open 和 `REDIS_ADDR` 选择路径。

### 模型调试多模态 embedding/rerank

- `POST /models/{id}/debug` 的 Embedding 分支不再拒绝图片。单图输入按 `parameters.spec.input` 或已知多模态模型启用，覆盖 Volcengine Ark 的 `/embeddings/multimodal`、DashScope 原生 multimodal endpoint、Gemini `batchEmbedContents`、Jina 的 `{"image": data_uri}` 输入以及 generic/vLLM 的 messages 输入。
- Ark、DashScope、Gemini 的文本侧也改走各自原生 embedding 协议；Ark 与融合型 DashScope 模型按单输入拆请求，避免把多个输入融合成一个向量。
- 响应解析覆盖 OpenAI `data[]`、Ark `data.embedding`、Gemini `embeddings[].values` 和 DashScope `output.embeddings[].embedding`；调试结果返回 `result_count` 与 `dimension`。
- `options.similarity=true` 时查询单独编码，文档和图片作为候选按请求顺序保留索引，返回 `kind/content/similarity` 并按相似度排序；候选维度与查询不一致时显式报错。
- 图片 rerank 覆盖 Jina/Cohere 的 image documents 与 NVIDIA NIM 的 image passages，NIM 的 logit 归一化为 0..1 相关度。
- 文本 rerank 新增 Hugging Face TEI 与 Pinecone Inference 原生协议：TEI 使用 `{query,texts,raw_scores:false,truncate:true}` 并读取裸数组，Pinecone 使用 `{model,query,documents,top_n,rank_fields,return_documents}` 并读取 `data[]`；两者都校验返回数量、索引、分数与重复项，并按 32/100 条分批及回填全局索引。`compat.extra_body` 可扩展非核心字段，TEI 的 `truncate` 可覆盖，核心查询/文档/分数/模型字段不可覆盖；Pinecone 自动附带 `X-Pinecone-Api-Version: 2026-07`。
- 视觉聊天模型现在会收到由图片自身向量召回的实际图片：`retrieval.context_images()` 只读取 `image_vector` 命中，按 `knowledge_base_id + image_url` 去重，最多 3 张，经 imageprep 按视觉模型 5 MB / PNG-JPEG-WebP-GIF 限制处理后转 data URI；读取或转换失败时保留 caption/OCR 继续回答。聊天上下文加入带安全边界的 `<retrieved_images>` 说明，用户自带图片排在前面。
- 自托管 `generic` 的 Qwen3-VL-Embedding 不再被模型名误判成 DashScope：文本与图片都走 OpenAI-compatible `messages`，默认启用二维会话批量（`batch_messages=true`），响应按 `index` 恢复输入顺序；单条输入保持一维会话，`compat.batch_messages=false` 可回退逐条请求。`max_image_batch_size` 默认 5，`extra_body`（如 `add_special_tokens`）会合入请求体。
- 校验与上游一致：文本和图片不能混用，非图片上传返回 `not an image`，不支持图片的模型返回 `does not accept images`，空输入返回 `input cannot be empty`。模型序列化的 `capabilities.input` 同步反映图片能力。
- 同一运行时接口的 worker pool 并发不再写死：依次读取 `asynq.core/postprocess/enrichment/maintenance/shared/wiki_concurrency` 系统设置、对应环境变量和默认值，并同步返回 `upstream_concurrency` / `parse_concurrency` / `wiki_concurrency`。

### 图片预处理 imageprep parity

- 新增 `app/services/image_prep.py`，统一处理发送给图片 embedding/rerank 的字节：先按文件头识别 PNG/JPEG/GIF/WebP/BMP/TIFF；格式在模型接受列表内且不超过 `max_image_bytes` 时原样透传。
- 需要转换时优先输出 JPEG（quality 85），模型不接受 JPEG 时回退 PNG；每轮按 0.75 缩放并使用 BILINEAR，最小边低于 64 时停止并返回 `image does not fit ... bytes even scaled down`。
- 带 alpha 的图片转 JPEG 时铺白底，避免透明区域变黑；空输入、PDF、文本和未知二进制分别返回 `image is empty`、`not an image (application/pdf)`、`not an image (text/plain)`、`not an image (application/octet-stream)`。
- `model_runtime.image_embedding()` 与 `image_rerank()` 均按模型 profile 的 `image_mime_types` / `max_image_bytes` 调用 prepare；Jina embedding 默认上限 5 MB，NVIDIA NIM rerank 默认接受 PNG/JPEG/WebP/GIF/BMP/TIFF。图片向量入库不再提前按 MIME 拒绝，统一由运行时校验并返回明确错误。
- 新增 `Pillow>=10` 运行依赖。

### docx/pptx 内嵌图片提取

- `chunking.extract_document()` 现在同时返回文本与图片资产；docx 读取 `word/document.xml` 及关系表，pptx 按页读取 `ppt/slides/slideN.xml` 及各自关系表，把图片按正文/幻灯片顺序写成 `embedded-image://N` 占位。
- 图片 caption 优先取 OOXML `docPr` / `cNvPr` 的 `descr`、`title`、`name`，缺失时回退媒体文件名；仅识别 PNG/JPEG/GIF/WebP/BMP/TIFF，EMF/SVG 等非位图资产跳过，不影响文本解析。
- 知识入库、单条重解析、批量重解析和移动 reparse 会把占位替换为知识自有引用：对象存储 KB 写入同知识前缀，本地 KB 写入 `<tenant>/<knowledge>/embedded/`；`image_info.extracted=true` 标记派生图片，重解析或删除知识时清理旧文件。
- `extract_text()` 保持纯文本语义，自动移除内部图片占位，避免 chat 临时文档和 URL 导入看到内部引用。

### PDF 内嵌图片提取

- `requirements.txt` / `pyproject.toml` 新增 `pypdf>=5`。`chunking._extract_pdf()` 用 pypdf 逐页提取文本和图片 XObject；无文字层的图片页不再被当成原始 PDF 二进制文本。
- 每页图片按 pypdf 返回顺序生成 `![Page N image M](embedded-image://K)` 占位并紧跟该页文本；仅保留 imageprep 能识别的位图 MIME，JPEG 2000 等不支持编码跳过，单页文本或单张图片读取失败不影响其他页。
- PDF 占位与 docx/pptx 共用后续链路：上传/重解析时物化到知识自有存储、生成 `image` 块，并可继续进入 VLM caption/OCR 与图片向量索引；重解析会清理旧 PDF 派生图片。

### EPUB 文本与图片提取

- `chunking.extract_document()` 已支持 `.epub`：用标准库解析 `META-INF/container.xml`、OPF manifest/spine，按 spine 顺序读取 XHTML；无 OPF 时回退排序后的 XHTML 文件。文本按块级元素换行，忽略 `head/script/style/template`。
- 图片别名同时保留 ZIP 中的字面路径与一次 URI 解码后的路径；解析 `<img src>` / SVG `<image href>` 时先尝试章节相对路径，再尝试归档根路径和 basename。图片文件名中的 `#`、`?`、`%xx`、编码过的反斜杠以及根目录/子目录同名冲突均不会互相覆盖。
- EPUB 图片沿用 `embedded-image://N` 占位和知识自有存储链路，可进入 `image` 子块、VLM caption/OCR 与图片向量索引；未引用的 manifest 图片只进入资产表，不会凭空写入正文。

### 音频 ASR 与视频拒绝

- 新增 `app/services/audio_asr.py`。KB `asr_config.enabled == true` 且 `model_id` 可见时，上传与重解析通过 `model_runtime.transcribe()` 调用 OpenAI-compatible `/audio/transcriptions`，将返回的 `text` 按普通文本分块入库；可选 `language` 会作为 multipart form 字段传给供应商。
- 音频未配置 ASR 时在上传入口直接返回 400“上传音频文件需要设置ASR语音识别模型”，不创建知识；重解析仍保留 failed 兜底。供应商调用失败（包括返回非法 JSON）创建 `failed` 知识并记录 `audio transcription failed: ...`，不产生文本 chunk；空转写按上游写入 `[No speech detected in audio file]` 并完成解析。
- 视频扩展名（mp4/mov/avi/mkv/webm/wmv/flv）在上传入口直接返回 400“暂不支持上传视频文件”，不创建知识；worker/reparse 仍保留“暂不支持视频文件”的 failed 兜底。模型调试 `/initialization/asr/check` 仍独立使用静音 WAV 做连通性测试。

### VLM caption/OCR

- 新增 `app/services/image_multimodal.py`。VLM 配置有效时才处理图片：新式配置要求 `vlm_config.enabled` 与 `model_id`，并兼容旧式 inline `model_name + base_url`；未启用、模型不可见或缺少 base URL 时直接跳过。上游已移除旧的 `image_processing_config.enable_multimodal` KB 总开关，Python 现按同一语义只检查 `vlm_config`。
- 每个图片块分别调用一次 caption 和 OCR。caption prompt 为 `Provide a brief and concise description of the main content of the image in <language>.`，语言取 `vlm_config.description_language`（缺省 English），并追加 `<image_description_business_instructions>`；OCR 使用固定的系统 prompt，不拼接用户自定义指令，避免业务规则破坏 `No text content` 输出契约。请求使用 OpenAI-compatible chat messages，图片以 `data:<mime>;base64,...` 的 `image_url` 传入，上限 5000 tokens；`finish_reason=length` 视为失败。
- OCR 返回在落库前统一校验：先剥离 Markdown 代码块和 HTML 包装，保留 `No text content` 等显式无文本回复；空骨架、无字母/数字/有效符号、超过 80% 的长周期重复片段均整段拒绝，不保留可能可信的前缀。分析结果记录 `ocr_status`（`succeeded`/`no_text`/`failed`/`skipped`）、`ocr_error_code`（`OCR_INVALID_OUTPUT`/`OCR_REQUEST_FAILED`/`OCR_TRUNCATED`）、`ocr_rejection_reason` 和 `ocr_raw_chars`；失败时清空 `ocr_text`，不会覆盖上一次结果或生成 OCR 子块。
- caption/OCR 成功后更新来源 `image` 块的 `image_info.caption` / `image_info.ocr_text`，并分别写入 `image_caption` / `image_ocr` 子块，`parent_chunk_id` 指向来源图片块，子块复用同一 `image_info`。重复处理按 `parent_chunk_id + chunk_type + image URL` 原地更新，不会累积重复子块；OCR 返回 `No text content` 时归一化为空且不创建空 OCR 子块。
- 图片读取、MIME 识别、单次 caption/OCR 调用或子块持久化失败均 best-effort 记录，不阻断文档主解析；成功生成的内容先于 wiki ingest 和图片向量索引落库，因此 wiki 摘要和 `image_vector` 候选可使用真实 caption/OCR。知识入库、单条重解析、批量重解析和移动 reparse 走同一刷新链路；无 VLM 配置时不产生 caption/OCR 子块。

### 知识文件上传准入

- `POST /knowledge-bases/{id}/knowledge/file` 现按 Go 入口只处理单个 `file`，成功响应 `data` 为知识对象而不是数组。上传前校验扩展名白名单，视频返回“暂不支持上传视频文件”，无扩展名返回“无法确定文件类型”，其它未支持扩展名返回“不支持的文件类型: <ext>”；FAQ 知识库返回“FAQ 知识库不支持文件上传，请使用 FAQ 导入功能”。
- 直接上传图片要求有效 `vlm_config`，音频要求 `asr_config.enabled + model_id`；两者均未配置时返回对应 400，不再先落 failed 知识。`MAX_FILE_SIZE_MB` 默认 50，超过时返回“文件大小不能超过NMB”。
- `.json` 上传在入口校验（空内容/非法 JSON），`metadata`、`process_config`、`enable_multimodel` 非法格式返回 400。`fileName` 支持 `docs/spec/design.md` 这类相对路径并拆分目录，标题保留完整文件名；`metadata`、`channel`、`folder_path` 落库。
- `process_config` 按 Go `KnowledgeProcessOverrides` 合并到本次上传：`chunking_config` 的尺寸/重叠/分隔符/策略等非零或非空字段覆盖 KB 默认值，VLM/ASR/问题生成配置整体替换（VLM 描述语言与自定义指令缺失时继承 KB），图片属性/向量开关和 extract 配置按字段合并。图片与音频准入、ASR 转写、分块入库、Wiki/图片索引均使用合并后的有效配置；覆盖写入知识 `metadata.process_overrides`。表单 `enable_multimodel` 仅在 `process_config` 未提供该字段时补入，显式覆盖优先。
- 单条重解析接受可选 `{"process_config": ...}`：提供时先按文件类型校验 VLM/ASR 准入，再替换知识 metadata 中的 `process_overrides`；未提供时沿用上传时保存的覆盖。单条、批量重解析和移动 `reparse` 模式都使用合并后的分块/VLM/ASR/图片向量配置。
- 同租户、同 KB、同文件名/类型/大小/SHA-256 的重复上传返回 409，响应含 `code=duplicate_file` 和已有知识；同时刷新已有知识的 `created_at`，不新增行。

### 生产图片多模态向量

- 新增迁移 `000041_chunk_embeddings`，为 `chunks` 增加 `embedding` 文本列；API 序列化会剔除该列，避免把原始向量暴露给前端。
- 新增 `app/services/image_vectors.py`：KB 必须同时满足 `indexing_strategy.vector_enabled != false` 与 `image_processing_config.image_vector_enabled == true` 才索引图片。扫描页（`metadata.image_source_type=scanned_pdf`）跳过；模型不可用或不接受图片时跳过；图片读取、非图片、尺寸/维度错误和上游调用失败只记录 `failed: ...`，不影响文档主解析。
- 图片候选来自 chunk `image_info`，支持 Markdown `![alt](url)`、HTML `<img src alt>`、docx/pptx/PDF 内嵌图片、本地/对象存储/`resource://`/`data:`/公网 http(s) 引用。Markdown/HTML/Office/PDF 图片引用会生成 `image` 子块并指向所属文本块；直接上传图片不再把二进制当文本，生成一个 `image` 块，缺少 VLM caption/OCR 时用文件名作为保守 caption。
- 每个候选调用一次图片 embedding，写入独立 `image_vector` 子块：`parent_chunk_id` 指向所属文本块（直接图片指向图片块），内容优先 caption、缺失回退 OCR，`image_info` 复用来源条目，`embedding` 保存图片自身向量，`status=2`、`flags=1`。重复索引按 `parent_chunk_id + metadata.image_url` 原地更新，并清理失效/重复的旧向量块。
- 检索侧对 `image_vector` 只计算查询向量与持久化图片向量的余弦相似度，命中阈值固定为 `0.2`；只有开启图片向量且当前 embedding 模型支持图片的 KB 参与。图片行不参与关键词分，避免 caption 与文本子块重复计分；返回引用新增 `chunk_type`、`source_type`。
- 入库、批量重解析、知识移动的 reparse 模式、`PUT /knowledge/image/{knowledge_id}/{chunk_id}`、分块删除和 `POST /knowledge-bases/{id}/rebuild-index` 都会刷新图片向量；KB 关闭开关后旧向量保留但检索侧不再召回，重新开启后可 rebuild-index 回填。

### 对话与 steer

- `delivery=inject` 在工具轮次边界注入。
- `delivery=after` 在原轮次结束后由服务端自动创建后续用户/助手消息并启动后台续跑；剩余队列消息自动绑定到新轮次。
- `delivery=inject` 的消费顺序已按上游 `932b750b2` 对齐：轮询只读取 queued 事件，用户消息插入与 `status='injected'` 的 CAS 在同一事务提交；任一步失败都回滚并保留事件，批次停在首条失败处，避免后续消息越级。
- 同一条 inject 连续 3 次持久化失败会标记为 `dropped`，后续消息才能继续；成功事件只在提交后发送 `user_message_injected`，并保留 `user_message_id` 与 `mentioned_items`。
- 后台续跑任务异常会记录日志，不被静默吞掉。

### API Key 请求认证

- `parse_auth` 支持 `X-API-Key`：Bearer JWT 有效时优先使用 JWT；Bearer 无效/用户失效时可回退到有效 API Key。
- Bearer JWT 同时校验 `auth_tokens` 中对应 access token 行存在且 `is_revoked=0`；登出、密码重置和用户禁用会立即让旧 access token 返回 401，与 Go `ValidateToken` 一致。
- 租户 Key 固定访问所属空间，携带不匹配的 `X-Tenant-ID` 返回 403；平台 Key 访问租户路由必须携带 `X-Tenant-ID`，缺失时返回 409。
- 撤销、过期和未知 Key 统一 401；`last_used_at` 按分钟节流更新。
- capability 路由授权与 Go 的 any-of 语义对齐，未声明的路由默认拒绝；全量租户 Key 可访问声明为 full-access 的租户路由，平台控制面只接受平台 Key 且必须显式携带对应系统 capability。
- KB allow-list 已作用于 KB/文档/分块/Wiki/初始化路径参数、知识库列表、批量查询、检索请求和聊天会话配置；越界 KB 返回 403。
- 租户 Key 创建/更新会归一化 capability 与 KB ID；全量 Key 会清空细粒度范围；平台 Key 必须至少携带一个已知 capability。
- `/files` 使用专用文件规则：全量 Key，或非 KB 限定且带 `retrieve` 的 Key 可访问；KB 限定 Key 与其他 capability 返回 403。
- 路由策略矩阵已复核：`/wechat` 使用 `manage_channels`，`/web-search/providers`、`POST /system/host-project-dir`、`/agent/tool-approvals`、`/user/favorites` 等路由保持默认拒绝。

### Go wire-shape parity 审计

- 用本机 Go 实例做黑盒基线，对比 29 个关键只读接口：28/29 状态码一致（`/tenants/1` 因 Go 无该固定 ID 而返回 403），25/29 字段树一致。
- 业务错误统一为 Go envelope `{"success":false,"error":{"code":...,"message":...,"details":null}}`；认证中间件、browser 不可用和 API Key 鉴权错误保留纯 `{"error":...}`。系统信息、管理员列表、租户列表、会话列表、审计游标、parser engines、我的邀请和 browser 空状态均按 Go 形状收敛。
- 用户对象移除多余的 `deleted_at`；租户默认 `retriever_engines` 改为 `{"engines":[]}`；`/system/info` 移除 Python 专属字段；`/system/parser-engines` 始终返回 `UnavailableReason`；`/me/invitations` 只返回 `invitations/total`。
- 跨空间访问统一受 `WEKNORA_TENANT_ENABLE_CROSS_TENANT_ACCESS` 与 `users.can_access_all_tenants` 双门禁约束；创建租户后，新租户的 active membership 可立即读取该空间，避免把“刚创建的成员关系”误判为越权。
- 内置 Agent catalog 已按 Go 完整对齐：4 个用户可见 Agent 的列表顺序与详情一致，`builtin-wiki-fixer`、`builtin-skill-installer` 作为内部 Agent 可单独读取；运行时仅替换 `tenant_id`。名称/描述按 `WEKNORA_LANGUAGE` → `Accept-Language` → `zh-CN` 覆盖默认、精确语言和语言前缀，租户落库的内置 Agent 优先使用数据库 `config`，名称/描述/头像按当前内置 catalog 覆盖，复制也沿用该配置；内部 Agent 即使落库也不会混入列表。自定义 Agent 响应不再暴露数据库专用的 `runnable_by_viewer`，`creator_name` 只在列表回填，单资源响应与 Go 一致省略。模型 provider catalog 已完整对齐 29 个 provider 的顺序、完整 base64 icon、`extraFields`、`models`、`credentialLabels`、`defaultUrls`、本地化 labels/descriptions 和操作能力。
- Agent 编辑器 catalog 已对齐：`/agents/placeholders` 返回 `all` 与 6 个字段分组的完整占位符；`/agents/type-presets` 从 Go 的 YAML 镜像加载 5 类预设，保留声明顺序、`config`/`kb_filter`，按 `WEKNORA_LANGUAGE` → `Accept-Language` → `zh-CN` 解析 i18n，并复刻 Go 的 false/空值 `omitempty` 行为。
- 推荐问题已按 Go 收敛：响应为 `{"questions":[{"question","source",...}]}`；starter 关闭返回空，`curated` 直接返回 agent_config 条目，`knowledge`/`hybrid` 从 FAQ、文档 chunk 生成问题和显式 Wiki 范围收集候选，支持显式 `limit`、agent starter count、30 条服务端上限与 hybrid 约 1/3 知识槽位；embed 端点复用同一解析器并保留 12 条上限，同时按 `WEKNORA_LANGUAGE` → `Accept-Language` → `zh-CN` 解析语言并转发 Agent 选中的 KB，使 Wiki fallback 可用。`tag_scopes` 会按 KB 校验标签，把文档标签解析为 knowledge IDs、把 FAQ 标签映射为本地 `seq_id`，无效标签不回退到 Agent KB；KB 限定 API key 会拒绝 `knowledge_ids` 与标签范围，并对隐式 Agent KB 取 allow-list 交集。
- IM 渠道读取已对齐 Go 的两种响应：`/agents/{id}/im-channels` 返回脱敏 `IMChannelSummary` 并带 `credentials_configured`，`/im-channels` 返回 `ChannelWithAgent`（不含凭证/KB/deleted_at），过滤已软删自定义 Agent、按 `created_at DESC` 排序并按 locale 重定位内置 Agent 名称；Create/Update/Toggle 仍按 Go 返回原始渠道。
- IM 渠道 Create/Update 已补请求校验：`platform` 必须属于 Go 的 10 个平台，`locale` 仅接受 `zh-CN/zh-TW/en-US/ko-KR/ja-JP/ru-RU`，非空 `knowledge_base_id` 必须属于当前租户且落在 KB 限定 API key 的 allow-list，`agent_id` 必须是内置 Agent 或当前租户的自定义 Agent；拒绝时统一返回 400。Create 现与 Go 一样返回 200 并保留空名称，WeChat 创建时按上游强制 `longpoll` + `full`。
- WeCom `websocket` 模式已具备真实运行时：`aibot_subscribe` 使用 `bot_id/bot_secret` 认证，30 秒 `ping`、90 秒无帧判定断线；断线按 1→30 秒指数退避重连，连续稳定超过 30 秒后重置。文本、语音转写文本和 mixed 文本可进入共享 IM Agent 会话链路，最终答案通过 `aibot_respond_msg` 的 replace 语义 stream frame 回复。应用 lifespan 启动/停止渠道，Create/Update/Toggle/Delete 会热同步；HTTP callback 与长连接共用会话、去重和 Agent 事件处理。
- 剩余差异均为运行数据而非契约缺口：`/tenants/1` 是固定 fixture 路径差异；示例知识库、环境向量库/存储实例是数据差异。

### 知识复制、移动与重解析任务

- `POST /knowledge-bases/copy`、`POST /knowledge/move`、`POST /knowledge/batch-reparse` 会写入 `py_task_results`、立即返回 `pending`，并由后台 worker 逐条处理。
- worker 只允许从 `pending/processing` 写入进度；系统管理员取消后不会被迟到更新覆盖。归档任务支持 `run_now` 重新调度。
- 复制任务记录已完成源文档 ID，移动任务按文档当前所属 KB 去重，因此中断后重跑不会重复复制或重复移动。
- 重解析任务优先从 `file_path` 重新抽取源文件；手工/URL 等无源文件文档使用现有 chunks 正文重新分块。成功后在同一事务内替换 chunks 并触发 Wiki ingest；无法抽取的文档标记 `parse_status=failed`，任务继续处理其余文档并在进度中记录 `failed/failed_ids`。
- 应用 lifespan 启动后会扫描 `pending/processing` 的 `kb_clone`、`knowledge_move`、`knowledge_reparse` 任务并重新调度，用于进程重启后恢复孤儿任务。
- 复制/移动按上游 `ValidateKBTransferCompatibility` 校验 source/target 不同、类型与嵌入模型一致；复制和 `reuse_vectors` 移动还要求向量库一致，所有转移都要求存储实例兼容。目标 KB 使用写权限校验。
- 移动请求强制 `source_kb_id`、`target_kb_id`、`mode`，去空白并去重知识 ID；每个文档必须属于 source 且 `parse_status=completed`。相同校验在 worker 执行前再次运行。
- `reuse_vectors` 移动保留原 chunk ID 与物理索引，只更新文档和 chunks 的目标 KB；`reparse` 优先读取源文件（无源文件时使用现有 chunks 正文），删除旧 chunks 后按目标 KB 的 `chunking_config` 重建并更新目标嵌入模型。两种模式都会删除标签关系、清理源 KB Wiki 引用，并在目标 KB 启用 Wiki 时重建页面；仅属于该文档的源页面软删除，共享页面移除对应来源和 chunk 引用。知识 ID 与文件路径保持不变。
- `GET /knowledge-bases/copy/progress/{task_id}` 与 `GET /knowledge/move/progress/{task_id}` 从任务表读取真实状态、总数、已处理数和消息，不再返回硬编码完成。
- 任务查询同时限定 `tenant_id` 与 `kind`；未知任务和跨租户探测统一返回 404。

### BrowserSkill 扩展通道

- 新增 `app/services/browser_skill.py`：单实例 daemon 生命周期、`daemon.json` 端口探测、Unix socket JSON-RPC、会话启动/暂停/停止、工具方法白名单和参数清洗。
- `/api/v1/local-browser/extension` 已实现真实 WebSocket 网关：Chrome 扩展 ID Origin、`bsk-auth.<token>` 子协议、设备令牌/历史令牌校验、握手 `instance_id` 重写、双向帧转发、45 秒租约心跳与断开释放；配对链接仅允许 HTTPS 或本机 HTTP。
- `local_browser` Agent 工具已接入 `POST /agent-chat/{session_id}`：自动创建/恢复服务端浏览器任务，注入可信 `session_id`，移除调用方伪造的 `session_id`/`browser_instance_id`。工具 schema 暴露完整字段并拒绝额外字段；派发前按方法校验允许字段、必填项、类型、枚举、范围、locator 唯一性与 emulate 互斥条件，`observe`/`snapshot` 的 `max_text_chars` 转换为 `max_tokens`。
- `preview`/`focus` 已通过上游 `ui.task_preview`/`ui.task_focus` 真实回传；UI 请求与自动化命令共用写锁。
- 轮次生命周期与 Go 对齐：成功的 `keep_open=false` 调用在 agent 轮次结束时回收任务；失败、取消或 `request_help` 保留任务，供下一轮恢复。
- 单会话删除和批量/全量删除会停止对应任务，并清理 `browser_task_interruptions`。
- 结果安全化：截图输出仅含尺寸/格式等元数据，原始图像保留在工具结果中并作为视觉消息送入支持视觉的模型；同轮多个工具调用时，图像统一追加在所有 tool 回复之后。非视觉模型会收到明确的不可查看提示。
- 普通 BrowserSkill 结果限制为 24000 字符首尾截断；daemon 错误保留 `code/message/data`，超大 details 只保留 `reason/effect_state` 等恢复信息，并生成结构化 `recovery_hint`。
- `recovery_hint` 已按 Go 分支补齐：`tab_select`/`tab_borrow` 的未授权 tab 文案、borrow 确认 UI 不可用、确认超时/取消、通用参数错误、selector 缺失、原生 select 约束、fill 状态冲突，以及 `effect_state=unknown/committed` 的“先观察、勿盲目重放”提示。
- 多副本 lease：`browser_devices.owner/owner_url` 记录连接节点，内部 `/local-browser/internal` 使用 HMAC-SHA256、60 秒时间窗和 nonce 防重放；工具调用、状态、控制、预览/聚焦、轮次结束和删除清理会转发到 lease owner。
- 结果契约与 Go 对齐：导航 `reached=timeout`、evaluate 非成功、request_help 未 continued/completed 都会判工具失败；显式暂停返回结构化 `task_paused`，必须经用户 resume 才能继续。
- 任务状态进度与 Go 对齐：`action`、`action_elapsed_ms`（执行中计时，结束后冻结）、`page_url`（仅 HTTP(S)，去除 userinfo/query/fragment，超长拒绝）、`last_error`（包含导航超时与 evaluate 失败详情，最多 1200 字符）以及人类步骤期间 `needs_help`/`help_prompt` 均写入状态快照。
- 人类步骤超时与 Go 对齐：`request_help`/`tab_borrow` 本地 daemon 与跨副本内部 RPC 均等待 315 秒，并限制 `timeout_ms`/`confirmation_timeout_ms` 在 1 到 300000 之间。
- 活动命令取消与 Go 对齐：pause、`session.user_interrupt` 和 stop 会根据任务记录的活动 RPC ID 向 daemon 发送 `cancel`；stop 先标记 stopping、暂停并取消活动调用，再在同一会话锁内执行 `session.stop`，避免旧命令继续执行或与新命令交错。
- Agent Window 收起保护与 Go 对齐：`tab_close` 前通过 `tab_list(scope=agent)` 检测是否只剩目标标签，若是则先用 `tab_create` 创建 `about:blank` 占位；主关闭失败/中断时仅在目标仍存在时 best-effort 关闭占位，防止 Chrome 随最后一个标签销毁窗口。
- `session.user_interrupt` 在网关消费并暂停任务；`session.window_closed` 会清空旧任务 ID，使显式 resume 创建新任务。RPC 超时或传输失败会向 daemon 发送对应 `cancel`。
- `BROWSERSKILL_MAX_CONNECTIONS` 已接入（默认 32）：扩展握手先预留容量，daemon/lease/accept/handshake 失败都会释放；正常注册后转为 live 占用，断开时归还。正在 stop 的任务会忽略迟到 `session.window_closed`，避免清理阶段被再次暂停。
- 跨副本内部 RPC 的错误 details 超过 8 KiB 时与 Go `RPCError.BoundDetails` 一致，只保留 `truncated` 以及不超过 128 字符的 `reason`/`effect_state`；`emulate.overrides` 与 `request_help.targets` 的嵌套字段类型/required 也已按 Go schema 校验。
- 应用关闭时会清除本节点持有的 `browser_devices.owner/owner_url/lease_until`，避免重启后被旧 owner lease 阻塞；其他节点的 lease 不受影响。
- 新增测试覆盖 daemon RPC 会话编排、工具参数清洗、WebSocket 握手/双向帧转发/UI 回复隔离/断线释放、窗口关闭任务重置和超时 cancel。

### Docker/Cube/E2B 远程沙箱

- 新增 `app/services/remote_sandbox.py`：使用标准库 HTTP 客户端实现 Cube/E2B 控制面 `health/create/exec/delete`，不依赖厂商 Go SDK。
- Cube 使用 JSON Connect，E2B 使用 protobuf Connect；数据面兼容 envd Basic 鉴权、traffic token、`X-Access-Token` 和 `proxy_url` 网关 Host 保留。
- 网络策略支持 `deny_egress_by_default`、`deny_out`、`allow_out`、Cube L7 rules 和 E2B host rules，并提供稳定的错误分类与中文原因。
- 管理端 sandbox deep check 已接入真实临时沙箱：执行探针、探测公网、销毁资源；策略禁止出网时返回 `egress_restricted_by_policy`。
- `POST /sandbox-configs/templates/query` 已读取真实模板目录：支持 `config_id` + 脱敏 `config` 合并已存凭据；Cube/E2B 读取 `GET /templates`，并实现 `ensure_standard` / `ensure_desktop` 与 `replace_standard` / `replace_desktop`。Cube 按 envd 规格构建/原地重建，E2B 通过 `v3/templates` 和 `v2/templates/{id}/builds/{build}` 两步构建；替换模板 ready 后会回写配置 `template_id` 并清理旧模板。
- 新增 `app/services/docker_templates.py`：Docker 模板目录改为读取真实 `docker image ls`，隐藏 skill/fork snapshot 和无关镜像，保留模板 label/当前配置镜像；配置镜像缺失时补 `missing` 项。`ensure_standard` / `replace_standard` 在后台线程执行 `docker pull`，同一 endpoint+image 的并发刷新复用进行中的 pull。
- E2B 模板列表增加状态校准：仅对第一方标准/桌面模板，在列表仍为 building/waiting 时查询当前 build status；buildID 为空且 build history 已有成功构建时返回 `untagged`，其余条目保持列表状态，避免全目录 N+1。
- 新增迁移 `migrations/sqlite/000037_session_sandboxes` 和 `app/services/session_sandbox.py`：会话首次使用时创建并绑定 Docker/Cube/E2B 沙箱，后续复用；支持远端命令执行、环境变量、工作目录与删除清理。
- 新增 `app/services/docker_sandbox.py`：每个会话用 `docker run -d` 创建常驻容器并通过 `docker exec` 执行；应用 CPU/内存/PID 上限、capability drop、`bridge/none` 网络、managed/idle TTL label，以及 0600 临时 env-file。执行前会 `start` 已停止容器、`unpause` 已暂停容器并轮询到 running；会话删除时 `docker rm -f`。Docker daemon 删除容器后 `inspect` 的 `no such object` 会映射为不存在；Docker desktop 返回 `desktop_unsupported`。
- Docker 空闲回收：create/exec 成功后按 daemon endpoint 限频触发后台 sweep，读取 activity marker mtime 与容器自身 TTL label，仅删除超过 TTL 的 managed 容器；容器已被回收时，session 绑定会检测并创建替代容器。会话单删、批量删除和 `delete_all` 都会在软删会话前清理绑定沙箱。
- Agent 新增 `shell_exec`，仅在 `allowed_tools` 显式允许且会话可解析 sandbox config 时注册；工具输出包含 stdout/stderr/exit code。
- `shell_exec` 可见输出改为 64 KiB 字节上限并保留头尾，不再复用 BrowserSkill 的 24000 字符截断；`ensure()` 落库失败时会删除刚创建的远端沙箱，成功后落库再尽力清理旧沙箱。
- 新增 120 秒 terminal ticket 与 `WS /sessions/{id}/sandbox/terminal`：票据绑定签发 access token 的 `auth_tokens.id`；校验票据类型、URL/票据 session 一致、token 未撤销、用户/会话归属和 `provision`，`ready` 返回 backend/`pty_id`/`reattachable`；二进制帧直接写入 PTY，文本帧处理 resize/ping，输出与输入共同刷新 15 分钟空闲窗口；每 60 秒复查鉴权，每会话单进程最多 5 个 PTY。
- 新增 `app/services/sandbox_terminal.py` 统一终端句柄；Docker 通过 Engine API `ExecCreate(Tty=true)`、升级连接和 `ExecResize` 提供原始 PTY 字节流，Close 时按 `WEKNORA_TERMINAL_ID` 清理 shell 进程树；Cube/E2B 通过 envd `Process.Start/Connect` 流式事件提供 PTY，支持 `SendInput`、`Process.Update` 和按 PID 重连。Docker 的 `pty_id` 恒为 0，Cube/E2B 仅在拿到远程 PID 时标记可重连。
- 新增 desktop ticket 与 `WS /sessions/{id}/sandbox/desktop`：在 sandbox 内执行镜像的 `start-desktop.sh`，读取 websockify secret，校验 `RFB 003.008` 版本帧，并通过 6080 数据面 WebSocket 中继 RFB 二进制帧；browser 帧上限 64 KiB，sandbox 帧上限 1 MiB。
- 新增 `000038_desktop_leases`：45 秒 SQLite lease、15 秒续租，跨副本保证单会话只允许一个 desktop；15 分钟空闲返回 `IDLE_DISCONNECTED`，每 60 秒复查用户/会话并返回 `AUTH_REVOKED`。
- 新增 `000039_sandbox_tickets`：terminal/desktop ticket 的 `jti` 入库并在 WebSocket 鉴权后原子消费；票据绑定 access token 行，撤销后握手和周期复查均返回 `AUTH_REVOKED`。
- 新增 `000040_desktop_last_sandboxes`：按会话记录最近桌面 sandbox ID（7 天 TTL），重连到不同 sandbox 时返回 `SANDBOX_REBUILT`。
- 补充权限边界：系统/配置只读端点要求登录；sandbox 配置、skills catalog、模型、存储、向量库、web-search、datasource、MCP 端点/服务及相关测试/同步/凭据写操作要求 Admin；附件、message artifacts 和 suggestions 统一校验会话所有者。
- 新增 `tests/test_remote_sandbox.py` 覆盖两种协议、鉴权失败、网络策略与 deep check 成功/跳过路径。

新增 `tests/test_docker_integration.py`（`WEKNORA_DOCKER_INTEGRATION=1` 开启），使用本地 `ikc-log-center:latest` 镜像真实验证 Docker Engine 的 create/exec/环境变量/TTY Exec+resize/stop 后自动启动/pause 后自动 unpause/删除和 managed idle sweep；测试结束确认无残留 managed 容器。Cube/E2B 仍只有协议级与 fake 流测试，尚未用真实云端账号完成 PTY/desktop 联调。每 60 秒复查 access token/用户/会话、15 分钟空闲断连和每会话 5 个 PTY 单进程上限已实现。desktop 已具备跨副本 lease、空闲断连、周期性重新认证、一次性 ticket、RFB 3.8 版本预检和 `SANDBOX_REBUILT` 提示；租约仍依赖共享 SQLite。

### 审批与 OAuth 共享状态

- 工具审批/OAuth 提示写入 `py_pending_approvals`；同进程仍用 Future 立即唤醒，其他实例通过数据库轮询拿到决议。
- MCP OAuth 的临时 state 与 authorization attempt 分别写入 `py_mcp_oauth_states`、`py_mcp_oauth_attempts`，回调可以在不同实例完成。
- 审批/OAuth 与 desktop lease 仍以项目现有 SQLite 数据库为共享介质；多实例需指向同一数据库文件。Redis 目前只用于后台模型并发限流，尚未接管 asynq 等价队列或通用审批状态。

### 外部 URL 安全

- 新增 `app/services/url_guard.py`：每次重定向重新解析，阻断 loopback/私网/链路本地/组播/保留地址、IPv4-mapped IPv6、6to4、Teredo 和 NAT64 内嵌私网地址。
- 连接固定到已校验 IP，HTTPS 仍按主机名做 SNI 与证书校验；限制重定向次数和响应大小。
- 已接入技能来源下载、URL 知识导入、RSS/Web 数据源抓取。自托管模型、Ollama、SearXNG、MCP 等合法私网端点未套用该防护。
- IM 出站回复改为复用 `url_guard.request_url`，覆盖 Feishu/WeCom/Mattermost/Telegram/通用 webhook 等 `_http_json` 调用；私有部署仍可通过 `SSRF_WHITELIST` 放行。

### DingTalk 数据源

- `app/services/dingtalk.py` 实现 Go connector 的在线文档子集：`oauth2/accessToken` 缓存与 401 刷新、workspace 30 条/节点 50 条/block 100 条分页、重复 `nextToken` 防护、workspace/folder/document 资源 ID、BFS 节点遍历和完整扫描后的删除回收。
- adoc Blocks 渲染覆盖段落、标题、引用、有序/无序列表、表格、代码、附件、callout/columns 和 unknown 容器；未知块类型记录到同步元数据，资源与文档 URL 使用 DingTalk 身份。同步写入 knowledge 时现在会合并条目级 `workspace_id/node_id/category/extension/unknown_block_types` 元数据，不再被通用同步字段覆盖丢弃。
- 数据源同步游标新增 `connector_cursor.resources` 映射，使 DingTalk 在完整扫描后只删除原选中作用域内消失的文档，扫描或读取失败时保留历史映射并推迟删除。
- `Validate` 已复刻 Go 的 workspace/node 采样边界：root 首页不占预算，共享 20 次后续 listing 预算并预留已排队 sibling；最多探测 5 个文档，失败后继续寻找可读文档；子目录不可读或预算尾部未覆盖时按 inconclusive 接受；root listing 失败按工作区聚合，命中探测上限且仍有未遍历节点时返回 “only the first 5 documents were probed”。
- 上传文件路径已接入：`include_uploaded_files=true` 时选择 `FILE/DOCUMENT` 的 docx/pptx/xlsx/pdf，先调用 `/v2.0/doc/dentries/{id}/queryDentryId` 解析钉盘 `spaceId/dentryId`，再查询 `downloadInfos/query` 获取短时预签名 URL，下载上限 64 MiB 且复用 URL Guard；文件内容通过 `chunking.extract_text` 转为纯文本 markdown 入库，空文本明确报错。Validate 对上传文件只解析 dentry 并查询下载 URL，不传输正文。
- 上传文件目前只保留纯文本，不保留内嵌图片或原始二进制；真实 DingTalk 租户凭据联调仍待执行。
- `tests/test_dingtalk.py` 覆盖分页/token 缓存、资源 ID 往返、块渲染、文件夹遍历、删除回收、元数据透传、失败探测后继续、inconclusive folder、探测上限、工作区失败继续、dentry/下载 URL、预签名下载、上传文件开关与文本提取；`tests/test_smoke.py` 覆盖 API 创建、同步、删除、知识软删及元数据落库。

### Seafile 数据源

- 新增 `app/services/seafile.py`：按 Go `seafile` connector 的 api2 协议实现 `base_url + api_token` 校验、`<repo_uuid>:<absolute path>` 资源 ID、单库约束、根路径折叠、资源祖先推导、库/目录懒加载和 `library/directory/file` 资源类型。
- 同步路径先 ping，再按一次 `GET /api2/repos/` 选中未加密库；递归扫描每个目录，文件按名称顺序下载。下载先走带 `Token` 的平台链接接口，再从 fileserver 无凭据读取；文件为空、大小不一致、链接被 SSRF 拦截、403/404 和目录扫描失败均有稳定错误分类。
- 指纹为 `o:<oid>|m:<mtime>|s:<size>`；完整扫描后才对账删除。普通同步只下载指纹变化文件，`sync_mode=full` 绕过连接器指纹跳过并由服务层强制重写已有知识；已知文件失败时保留空指纹待重试，未知失败不写入游标。游标持久化 `repo_id/repo_name`，即使库为空也能在发起 HTTP 请求前拒绝切换库。
- `datasource` 注册、资源祖先路由、同步管线的 `file_name/file_type` 透传、错误项指纹保留、列表响应隐藏 `last_sync_cursor` 已接入；复用 `url_guard` 的固定 IP 与重定向 SSRF 防护。
- `tests/test_seafile.py` 覆盖资源 ID/路径、库与目录浏览、Validate 不列目录、递归同步、增量跳过、强制全量重下、空库游标绑定、删除对账、库切换拒绝、错误码、重试与 `Retry-After` 上限；`tests/test_smoke.py` 覆盖 API 创建、同步、全量更新、资源树、删除对账、游标隐藏和元数据落库。
- 当前同步只为 `chunking.extract_text` 支持的文本/Office/PDF/EPUB 等格式提取正文；图片、音频及其他二进制导入类型会在条目级报错，真实 Seafile 10.0.1 租户联调仍待执行。

### 文档摘要

- `POST /knowledge/{id}/regenerate-summary` 已优先解析 KB `summary_model_id` 对应的 chat runtime，使用结构化 profile 提示词生成摘要；输入按 24000 字符头尾采样，并按 KB `description_language` 或 `Accept-Language` 选择输出语言。
- `app/services/document_summary.py` 严格解析 `summary`/`gist`，兼容 BOM 与 markdown 围栏；JSON 形状但截断/畸形的输出返回不可用，不会把模型原文写入 `description`，非 JSON 纯文本仍保留兼容兜底。
- 创建、单文档重解析和批量重解析现已统一入队 `summary_generation`：默认 `summary_enabled=true`，只对解析完成且含至少 10 个非空白文本字符的文档触发；`process_overrides.summary_enabled=false` 会跳过。任务写入 `py_task_results`，复用 300 秒租约、60 秒续租、启动恢复与 3 次重试，并支持运行时队列的取消/重跑。
- 摘要 worker 只更新 `description/summary_status/updated_at`，开始时置 `processing`；成功写 `description` 与 `completed`，终态失败写首个文本 chunk 的前 500 字符与 `failed`，非终态失败保留旧描述并置 `pending`。生成期间 chunk 内容、启用状态、顺序或 `custom_metadata` 变化时丢弃过期结果，避免覆盖重解析后的新任务。
- 模型不可用时直接发布本地兜底，不进行无意义重试；模型调用失败或输出不可解析沿用重试预算，截断/畸形 JSON 不会写入 `description`。`regenerate-summary` 的同步行为保持不变。

### WeCom 长连接跨进程租约

- 迁移 `000042_im_channel_leases` 新增 `py_im_channel_leases`；`im_leases` 以单条 UPSERT 实现 15 秒原子租约，只有过期或自身持有的租约可被更新。
- `im_runtime` 每 5 秒续租，非 leader 每 10 秒重新读取频道并尝试接管；续租失败会停止当前适配器并进入接管循环，进程关闭时释放租约。
- CRUD 热同步会先停止旧运行时并释放租约，避免同一进程内重复订阅；适配器异常退出也会释放租约并重新参与接管。
- 测试覆盖租约互斥/释放、第二实例等待、续租丢失后停止适配器和运行时启停同步。

### WeCom 图片与文件消息

- 新增 `app/services/im_attachments.py`：下载 URL 复用 `url_guard.request_url` 的固定 IP、重定向和 SSRF 校验，响应上限 32 MiB；按消息 `aeskey` 做 base64（含无 padding）解码，使用 AES-256-CBC、首 16 字节 IV 和宽松 PKCS#7 去填充。
- 图片按文件头识别真实 MIME，超过 8 MiB 时不生成视觉 data URI；文件按原扩展名或 PDF/ZIP 文件头/UTF-8 探测扩展名，复用 `chunking.extract_text` 解析 docx/xlsx/pptx/pdf/文本，正文按 32 KiB 和 500 行截断。
- 附件元数据（名称、类型、大小、行数、截断状态、解析文本）写入用户消息 `attachments`；`im_handler` 为无 caption 的图片/文件生成稳定查询，`stream_chat_events` 将附件内容作为不可信资料块注入，并把图片 data URI 传给支持视觉的聊天/Agent 模型。
- 渠道配置 `knowledge_base_id` 时，已下载字节会在后台复用 HTTP 文件上传的存储、提取、分块与哈希去重链路入库；持久化在独立任务中执行，不支持的类型、重复文件或存储异常只记录日志，不改变或延迟原问答回复。
- Yunzhijia 附件下载在连接/读取超时时按 500ms 间隔重试一次；非超时错误、第二次超时和其他 IM 平台仍直接失败。`im_handler` 会把渠道平台带入附件准备链路，避免改变通用下载行为。
- `requirements.txt` / `pyproject.toml` 新增 `cryptography>=42`。
- 测试覆盖无 padding AES key、错误 key、JPEG 实际 MIME、无扩展名文本文件、附件提示词转义、聊天附件持久化/视觉入模、IM 图片消息完整下载到问答链路，以及知识库后台入库、未配置知识库不保存、重复文件幂等、不支持类型和入库异常不破坏回复。视频仍明确回复不支持。

### 上游近期提交审计

- `643f64180` 平台用户管理：Python 已有列表、状态切换、自禁用/最后管理员保护、禁用撤销 token 与每请求 `is_active` 复查；本轮补上非法 `offset/limit` 回退默认值及 `limit=200` 上限测试。
- `00474f734` 迁移对象容错：SQLite 对应脚本中的 browser/memory/model catalog 表与索引定义改为 `IF NOT EXISTS`，model catalog 默认行使用幂等插入；含 `ALTER TABLE` 的脚本仍依赖迁移记录只执行一次。
- `57f8a65fe` DingTalk `Validate` 采样边界：Python 已复刻 workspace root/分页 listing 预算、最多 5 次文档探测、探测失败后继续、inconclusive 接受、失败工作区聚合和探测上限错误提示。
- `d9237166c` WeCom 自定义 `wss` 端点：已补齐 WeCom intelligent-bot 长连接客户端，启动时扫描启用中的 `wecom/websocket` 渠道，订阅、30 秒 heartbeat、90 秒读超时、指数重连、`aibot_respond_msg` 与 CRUD 后渠道热启停均已接入；自定义 `ws_endpoint` 必须为 `wss`，并复用 SSRF 白名单校验。
- `5343ac47b` / `9efe94cc6` Claude 5 系列：Python 运行时已具备 adaptive effort、`between_tools` 和 no-sampling 分支，但目录条目缺少 `compat`，实际请求会退回 budget thinking；本轮补齐全部 Anthropic 模型兼容字段，并新增从目录配置生成请求体的集成测试。
- `89e3273da` MCP 服务创建：确认并固化“缺省启用、显式 false 保留”，同时把创建/单资源响应的 `enabled` 从 SQLite 整数统一为布尔；新增创建、读取与列表回归测试。
- `0d39af121` summarize 意图跳过 KB 检索：已补齐 `QueryIntent` 分类器与检索门禁。`enable_rewrite` 或图片触发查询理解，解析 `rewrite_query`/`intent`，`summarize` 等非检索意图跳过 KB 检索，`kb_search`/clarification/空意图使用改写问题检索；未知标签和模型失败回退原问题并检索。同步覆盖意图归一化、`NeedsKBRetrieval`/`NeedsRetrieval`、结构化输出解析和截断恢复。
- `40eb34bc8` web_search 数量上限：Python 工具 schema 的 `limit.maximum` 改为按 agent `web_search_max_results` 动态生成（默认 5、最高 20），省略时使用该上限；超上限截断并在输出中提示，`limit < 1` 或非整数在调用 provider 前拒绝。
- `d27ecade6` OCR 无效输出拒绝与失败记录：Python 在 `analyze_image()` 中对原始 OCR 做显式无文本识别、可读性和长重复检测；拒绝结果不写入 `image_info.ocr_text`，并返回 `OCR_INVALID_OUTPUT`、`OCR_REQUEST_FAILED` 或 `OCR_TRUNCATED` 及拒绝原因。OCR prompt 同步改为“只转写可见文本、散落 UI 文本按行输出、仅真实表格用 Markdown 表格”，避免模型以空表格或说明文字填充。
- `5f18308b0` 分离重复片段与 HTML 无文本回复：重复检测改为统计多个周期长游程的并集覆盖率，要求重复片段至少占 80%，避免不同分隔符循环或重叠周期重复计数；HTML 包裹的显式无文本回复在短文本过滤前保留，并以 `no_text` 结束而不是校验失败。
- `2a02807a6` Markdown 围栏与标题检测：Python 原有实现只按前缀切换 ```/~~~，会把 4 空格缩进、带尾随文本的闭合符和不同长度闭合符误判。本轮加入状态机，按 0–3 空格缩进、标记长度、同 marker、闭合长度和纯空白尾随做 CommonMark 判定；profile、heading boundary、heading entries 与 heuristic 扫描共用该状态，代码块内伪标题不再影响分块。
- `09411106f` 租户管理员管理频道会话：更新、单删和批量删除在严格 owner 作用域失败后，允许 Web 租户 Admin/Owner 访问同租户的 IM、Embed、MCP 及频道 owner 前缀会话；普通用户私有 Web 会话、跨租户 ID 和受限 API Key 仍返回 404。批量删除按实际 owner 清理浏览器作用域，全部不可见返回 404。
- `8125ed250` 保留知识引用元数据：检索 SQL 补充知识描述、来源、通道和自定义元数据，`references()` 输出 `knowledge_filename`、`knowledge_source`、`knowledge_channel`、`knowledge_description`、`knowledge_custom_metadata`、`chunk_index` 与 `parent_chunk_id`；自定义元数据按 key 稳定排序转成 `key: value` 文本。
- `cbfc0fed4` 标注 FILTER_TOP_K 子集截断：`retrieve` 返回记录候选池大小的列表子类，聊天在候选数大于实际注入数时追加 `<subset shown="N" candidates="M">` 说明；知识 ID 过滤改为 SQL 条件，避免候选池计数被截断后过滤污染。
- `005627b18` / `11aa00f85` 工具参数类型：Python 直接保留 `json.loads` 的整数/浮点/布尔/数组/对象类型，调用工具前没有 Go 那样的字符串化 enum 比较或全量 float64 重编码，因此两个缺陷在 Python 路径中不适用；对象 key 顺序和未声明字段也不会被重写。
- `d074ec7b0` Metaso 非网页 scope：Python 已按 `scope` 选择 `webpages/documents/scholars/podcasts/videos/images` 数组，现有测试覆盖 document scope；本轮复核无需移植。
- `419f17fc6` Wiki 反链标题：Python 的详情序列化已为 `in_links` 解析同 KB 可见页面标题并输出 `in_link_titles`，本轮复核无需移植。
- `b1d92048c` 读取分页路径预算：Python 的 `_render_file_page` 已把清理后的文件路径 rune 数计入输出预算，路径过大时拒绝读取且不返回 `next_offset`，已有长路径分页测试覆盖。
- `dccb1d6d2` MCP 只读工具注解：Python 端点工具目录已为 8 个只读工具统一返回 `readOnly=true/destructive=false/idempotent=true/openWorld=false`，`ask` 与写文档工具保持 destructive，本轮复核无需移植。
- `be581c41e` MCP OAuth 按授权用户关闭连接：Python 的 `MCPClient` 是单次工具调用内创建并在 `finally` 中关闭，没有共享连接池，因此不会因一个用户的 OAuth 关闭影响其他用户；已审计为无需移植。
- `3d01bebce` EPUB 图片相对章节解析：Python 原先仅在扩展名白名单中接受 `.epub`，实际提取会落为 “No extractable text found”。本轮补齐标准库 EPUB 解析、spine 顺序、章节相对图片解析和字面 `#/?/%xx` 路径保留，并用上游同类的 12 组路径冲突用例验证。
- `5d40f81ee` Yunzhijia 附件超时重试：Python 通用附件下载新增平台条件重试，仅 Yunzhijia 在超时后间隔 500ms 重试一次；第二次仍超时、非超时错误和其他平台不重试。
- `e372b8e42` Qwen3-VL 消息批量：Python 现已识别 generic 自托管 Qwen3-VL，默认二维 `messages` 批量文本/图片请求，按响应 `index` 重排，并支持 `batch_messages=false` 与 `max_image_batch_size`；图片索引按批调用，坏批只标记该批候选。
- `aa535c22f` 排队追问串流归属：复用构建产物已包含 `agent_query` 仅绑定“尚无 request_id 或 request_id 相同”的助手占位条件，避免旧轮次被新追问重新贴标签；前端源码不在本仓库重建。
- `de616ed4b` RRF 列表到达顺序：该提交本身只新增回归测试，Python 检索是单 SQL 候选集上的词法/向量线性混合分数排序，没有 Go 的多列表 `bestRanks`/RRF 融合路径，因此不适用。
- `d6269994b` 出站拨号超时：`url_guard` 的固定 IP HTTP/HTTPS 连接默认 5 秒拨号超时，可用 `WEKNORA_OUTBOUND_DIAL_TIMEOUT`（支持 ns/us/ms/s/m/h）配置；建立连接后恢复原响应读取超时。
- `99b4c3a9b` Hugging Face TEI rerank：Python 现按 provider `huggingface_tei` 发送原生 `query/texts` 请求，默认 `raw_scores=false`、`truncate=true`，读取裸 `{index,score}[]`；按 `max_documents`（默认 32）分批，校验数量/索引/分数并回填全局索引，`extra_body` 不能覆盖核心字段。
- `5a25e2614` Pinecone Inference rerank：Python 现按 provider `pinecone` 发送对象 documents、`rank_fields` 和 `top_n`，读取 `data[{index,score}]`；目录为两个模型补齐 `max_documents=100`、`truncate=END`，请求携带 `X-Pinecone-Api-Version: 2026-07`，并按 100 条分批及回填索引。
- `ea7cf8d5d` assistant 工具调用历史的空 content：OpenAI-compatible 请求在序列化前补齐 `role=assistant + tool_calls + 空/缺失 content` 为 `"content": ""`，且不修改调用方原消息；普通 assistant 缺省 content 仍保持省略，避免 strict vLLM/Xinference Jinja 模板把缺失字段当 null 崩溃。
- `3fb6b9d63` 知识库更新 VLM 配置：`PUT /knowledge-bases/{id}` 现接受顶层 `vlm_config`（缺省/null 不变），仅写入 `enabled/model_id/description_language/custom_instructions`；模型必须存在且为 `VLLM`，指令上限 4000 字符。请求中的 legacy `model_name/base_url/api_key/interface_type` 只用于判定 GET-then-PUT 回显，绝不落库；启用托管模型或未回显地关闭会清空 legacy 端点，纯 legacy KB 的回显/无 model_id 启用保持原配置。
- `0466c294e` 模型流建立阶段空闲超时：Python 的 8080 API 不消费上游 `ChatStream`，而是在阻塞的 chat completion 返回后由路由生成 SSE 帧；没有 Go 的 stream 建立/首 chunk 看门狗分支，因此该提交不适用。
- `65f88f412` 图片命中按图片重排并展示给视觉模型：Python 没有 Go 的模型 rerank/`kept_by` 管线，图片命中直接按向量分数参与检索，因此“文本 rerank 无法判断图片”这一半不适用；本轮补齐可适用的一半，即图片向量命中按自身图片读取、去重、imageprep 后最多 3 张交给视觉聊天模型，并保留 caption 作为文本证据。后续检索已补 MMR，图片命中同样参与 `Token` 集合去冗余。
- `95691daab` 索引失败后文档任务重试：Python 检索运行时是 SQLite，分块与向量同库写入，远端 embedding 失败时 `embed_texts` 回退本地 embedding，不存在 Go `BatchIndex` 失败后 ack 导致永久 failed 的异步队列路径；该提交不适用。
- `bea0d7b68` PPTX fallback 图片关联：Python 不经过 MarkItDown 的占位符回填，而是直接读取 slide XML 与关系表、按文内顺序解析 `r:embed`；每个 slide 使用自己的 `.rels`，不存在 fallback 队列换位问题，该提交不适用。
- `1826f46b1` 云解析轮询超时配置：Python 的 `weknoracloud` 仅实现 APPID/APPSECRET 校验与状态，文档解析仍走本地 `chunking.extract_document()`，没有 PaddleOCR-VL Cloud / WeKnoraCloud docreader 轮询任务，该提交不适用。

### 测试

- 新增/更新 smoke 测试覆盖：
  - 分块预览策略、偏移、限制和截断
  - FAQ 导入进度与显示状态
  - FAQ 异步导入、dry-run、replace 和进度轮询
  - 运行时队列统计、状态查询、取消/删除/归档清理
  - FAQ 任务取消后不写入条目且保持 cancelled
  - `POST /knowledge/tags`
  - `after` steer 自动续跑
  - Wiki 队列统计
  - 技能目录、技能来源解析、受保护文件代理
  - BrowserSkill 网关、daemon 会话编排和 preview UI 回传
  - BrowserSkill 轮次回收、`keep_open` 保留和会话删除清理
  - BrowserSkill 截图元数据/视觉消息、结果截断和结构化错误
  - BrowserSkill 多副本 owner 路由、内部 RPC 签名与 nonce 防重放
  - BrowserSkill 导航/evaluate/request_help 失败语义与 task_paused 恢复约束
  - BrowserSkill 任务状态进度、耗时冻结、安全 page_url 与人类步骤提示
  - BrowserSkill pause/interrupt/stop 对活动 daemon RPC 的取消与串行停止
  - BrowserSkill `tab_close` 最后标签占位保护与失败清理
  - BrowserSkill 扁平参数 schema/方法级校验与 `max_text_chars` → `max_tokens` 映射
  - BrowserSkill 人类步骤 315 秒超时与参数边界
  - BrowserSkill Chrome 扩展 Origin/安全配对链接、窗口关闭任务重置、网关双向帧/UI 隔离和 RPC 超时 cancel
  - Cube/E2B 协议、鉴权、网络策略与 deep check
  - Cube/E2B 模板目录、ensure/replace 构建、失败模板原地重建、E2B 两步构建与替换后配置回写
  - Docker 真实镜像目录、快照/无关镜像过滤、missing 项、后台 pull 复用
  - Docker 会话常驻容器、执行/恢复/删除、资源与网络参数、env-file 权限与清理
  - Docker idle sweep、TTL/marker 判定、被回收绑定自动重建
  - Docker exec 与 idle sweep 竞态：非零退出后复核容器，确认消失时归类 `not_found` 并保留退出码；容器仍存活时保持真实命令失败
  - Docker 真实 Engine create/exec/环境变量/TTY Exec+resize/stop 自动启动/pause 自动 unpause/删除/idle sweep、模板目录和后台 pull 失败状态（本地 daemon/镜像，opt-in）
  - 会话单删/批量删除/delete_all 清理绑定沙箱，批量结果只统计可见会话
  - 向量库注册/CRUD 契约（7 个外部引擎、字段 schema、敏感/immutable/enum 元数据、Tencent replica 环境默认值、env store 只读/测试、创建校验/SSRF/重复检测/版本回写、仅改名更新与 KB 绑定删除保护）
  - Web 搜索 14 provider 元数据契约，以及 Serply/Baidu/Keenable/Metaso/Zhipu/Exa/Bocha 的 URL、鉴权头、请求体、默认值和响应映射
  - Web 搜索保存时参数校验、显式代理请求、重定向校验、SSRF 白名单精确/通配/IP/CIDR 匹配和 SearXNG 默认参数
  - `ssrf.whitelist` 数据库设置/string-list 写入、环境变量回退、固定 IP 请求器和无代理 POST 请求
  - 后台模型并发限流、等待队列、运行时统计与交互请求放行；Redis 跨实例限额、心跳、租约回收与 fail-open（真实 `redis-server`，opt-in）
  - 多模态 embedding 的原生文本侧（Ark/DashScope/Gemini）、图片侧 5 类协议 URL/请求体/响应解析，以及混用、非图片、不支持和空输入错误路径
  - 图片 embedding 相似度的查询/文档/图片编码顺序、候选索引与维度不一致错误
  - 图片 rerank 的 Jina/Cohere 与 NVIDIA NIM 请求体、响应解析和 logit 归一化
  - 文本 rerank 的 TEI/Pinecone 原生请求体、裸数组/data 响应校验、32/100 分批、全局索引回填、extra_body 核心字段保护与 Pinecone 版本头
  - OpenAI assistant 工具调用历史显式空 content、调用方消息不被修改，以及普通 assistant 保持省略
  - 知识库更新 VLM 配置的托管模型校验、4000 字符上限、legacy 字段拒绝写入、纯 legacy 回显与关闭清理
  - 图片向量上下文：仅选择 image_vector 命中、按 KB+URL 去重、最多 3 张、视觉模型聊天消息附加 data URI 与安全上下文说明
  - imageprep 原样透传、JPEG/PNG 转换、透明铺白、逐轮缩放、最小边限制、不可用格式与空/PDF/文本错误
  - 图片 embedding/rerank 请求体使用 imageprep 后的 MIME、base64 与大小上限
  - docx/pptx 关系表解析、文内顺序与 alt/descr caption、内嵌图片落盘、image 子块/image_vector 生成、reparse 后文件重建与旧文件清理
  - PDF 逐页文本/图片提取、页面顺序占位、无文字层不回落原始二进制、上传落盘与 reparse 后派生图片替换
  - EPUB OPF spine 顺序、章节相对图片、根/子目录同名冲突、字面 `#/?/%xx` 与编码分隔符、`extract_text()` 去除占位
  - Qwen3-VL/generic `text_as_messages`、二维 `messages` 批量、响应 index 重排、单条/显式关闭逐条、图片按 5 张分批与 `extra_body`
  - Yunzhijia 超时重试一次、第二次超时不无限重试、非超时错误与其他平台不重试
  - 出站拨号默认 5 秒、Go duration 环境变量解析、连接后恢复读取超时
  - 音频上传、ASR 模型配置与 language form 字段、`/audio/transcriptions` multipart 请求、重解析重新转写；未配置 ASR 与视频的上传前 400、供应商非法响应落 failed 且不产生 chunk
  - 知识文件上传准入：扩展名/无扩展名、空/非法 JSON、`MAX_FILE_SIZE_MB`、FAQ 拒绝、`fileName` 目录拆分、metadata/channel 落库、重复文件 409 及单对象响应；`process_config` 分块覆盖、VLM/ASR 单次准入、表单优先级、非法字段类型，以及上传覆盖在重解析中的沿用/替换与准入拒绝
  - 图片向量入库：Markdown/HTML 图片引用生成 image 子块、直接图片上传、caption/OCR 内容选择、image_vector 父块与持久化 embedding、重复索引去重与删除刷新
  - 图片向量检索：KB 开关、图片模型能力、0.2 阈值、图片不参与关键词分、引用返回 chunk_type/source_type
  - 图片向量 best-effort 边界：未开启 KB 不调用模型、维度不一致、扫描页、非图片 PDF 与文本-only 模型不产生向量块
  - VLM caption/OCR：新式/旧式配置解析、caption 语言与自定义指令、OCR 固定 prompt 与 `No text content` 归一化、data URI 请求、来源 `image_info` 更新、caption/OCR 子块父子关系与重复处理幂等、VLM 失败不阻断主解析
  - OCR 校验：代码块/HTML 包装、HTML 包裹的无文本回复、空骨架/不可读内容、单词与分离片段长重复、含大量真实表格时保留、长度不足不误判、失败清空 OCR、请求失败/截断错误码与 `ocr_status` 记录
  - Markdown 围栏：backtick/tilde、长短标记、0–3 空格缩进、带文本闭合符、错误 marker、制表符/4 空格伪闭合、backtick info string、未闭合围栏，以及代码块后标题 offset 不漂移
  - 会话管理作用域：Admin/Owner 管理 IM/Embed 频道会话、普通 Web 会话不可见、跨租户 404、批量删除混合可见项与按 owner 清理浏览器作用域
  - 检索引用与截断：知识文件名/来源/通道/描述/自定义元数据、chunk 序号/父块、稳定元数据排序，以及 subset 仅在候选池大于展示数时生成
  - 初始化配置多模态与存储：旧 `image_processing_config.enable_multimodal` 不再作为开关，`multimodal.enabled` 按 VLM 配置、legacy `cos_config` 凭据/桶或非 `local` 存储 provider 计算；响应按实际存储输出 `storageType` 及 `cos`/`minio` 详情（共享接收方隐藏桶与区域）；PUT 不再回写旧开关，并校验/持久化 `storageProvider`、`storageBackendId`，已有文件时禁止切换存储后端。PUT 按完整配置语义重建 VLM/ASR：仅显式启用且模型可见时保留，关闭或模型不存在时清空模型 ID，VLM 的描述语言与自定义指令仍按请求保留。同租户 Viewer 与受限 API Key 不返回模型 baseUrl/存储详情，builtin 模型始终隐藏 baseUrl 与 API Key；LLM/Embedding 模型需可见，已有文件时禁止切换 Embedding 模型；questionGeneration 启用时数量限制 1–10
  - 运行时队列 worker pool 并发设置、环境变量回退与汇总字段
  - E2B stale pending build status、build history 与 untagged 状态校准
  - 会话级 Docker/Cube/E2B 绑定、环境变量、命令执行与 `shell_exec` 工具
  - Agent `read_file` 工作区相对/绝对路径、skill:// 复用、目录/二进制/8 MiB 拒绝、长路径输出预算与多页无损重建
  - Agent `list_sandbox_files` 默认输出目录、相对路径、500 条上限、NUL 分隔 find 解析、排序与聊天工具注册
  - Agent `write_sandbox_file` / `edit_sandbox_file` 覆盖 overwrite/append、输入目录与目录根拒绝、二进制/大小限制、原始内容批量编辑、唯一/全量/重叠校验、失败不落盘、base64 分块上传与临时文件清理
  - 查询理解与意图门禁：意图归一化、`NeedsKBRetrieval`/`NeedsRetrieval`、JSON/markdown/截断解析、`summarize` 跳过检索、`kb_search` 使用改写查询、失败回退与 `enable_rewrite` 关闭时不调用模型
  - web_search 动态上限：配置上限写入 schema、默认使用上限、超上限截断提示、非正数/非整数拒绝且不调用 provider
  - terminal ticket、WebSocket session 绑定、provision、ready/pty_id/reattachable、原始字节输入、resize、Docker Engine hijack、Cube/E2B envd PTY 帧解析与退出码
  - terminal access token 撤销复查、会话删除复查、每会话 5 个 PTY 上限与关闭后计数释放
  - desktop 启动脚本、secret 解析、6080 WebSocket 目标、RFB 3.8 预检和双向中继
  - desktop 跨副本 lease、空闲断连、周期性认证、busy 关闭码与 ticket 重放拒绝
  - desktop sandbox 重建提示、`run_now` FAQ 重跑、附件/artifact/suggestions 鉴权与 sandbox Admin 权限
  - Viewer 对租户级基础设施、组织、租户成员/API key、Agent/IM 渠道、evaluation，以及 KB/文档/分块/标签/FAQ/Wiki 写接口和文件下载的统一 403 权限矩阵，以及对应只读接口仍可访问
  - Contributor 对他人 KB、文档、分块、Wiki、标签、FAQ 和初始化配置的写操作统一返回 403
  - 标签、FAQ、API key 和 IM 渠道的跨租户更新统一返回 404，且不泄露或修改目标租户数据
  - 文档与 FAQ 引用他租户标签时返回错误，跨租户标签关系不会落库
  - 组织共享 KB 可读取标签、FAQ，并按源 KB 租户解析初始化配置；共享接收方不获得集成地址，共享 Viewer 无法下载源文件
  - FAQ 导入使用标签 `seq_id`，带标签条目可正常导入并持久化
  - API Key 有效/撤销/过期、Bearer 回退、平台 Key `X-Tenant-ID`、capability 不足、路由默认拒绝和 KB allow-list 越权
  - API Key 专用 `/files` 规则与路由策略矩阵（wechat、web-search、host-project-dir、审批/个人路由等）
  - Go wire-shape：标准错误 envelope、认证纯错误、列表/游标/browser 空状态、用户/租户/解析器/系统信息字段、跨空间双门禁与租户创建后成员访问
  - 系统用户状态：`user_id`/布尔 `is_active` 必填校验、缺失或软删用户 404、自禁用与最后活跃系统管理员 400、成功响应含脱敏 `user`，禁用后旧 token 立即失效且重新启用不恢复旧会话
  - Bearer 撤销强制：每次请求校验 access token 对应的 `auth_tokens` 行存在且未撤销，登出、密码重置和用户禁用后旧 token 立即失效
  - 模型 catalog 解析与 Anthropic 原生 Messages：精确 catalog 条目及 API/model 元数据、文本/系统消息、Base64/远程图片、工具定义与 `tool_use`/`tool_result`、OpenAI 形状响应转换、usage/stop reason 映射、prompt 总量计入 cache read/write，以及 Claude 拒绝采样参数时省略 `temperature`/`top_p`（非流式）
  - Anthropic thinking：支持 `reasoning_effort`/legacy `thinking` 解析、模型 level clamp、budget/adaptive 两种 `thinking` body、`output_config.effort`、thinking budget 与 `max_tokens` 协调，以及带签名的 thinking/redacted_thinking block 在工具循环消息中的原样回放；同步非流式传输保持不变
  - Wiki 图谱子集：index 页默认排除、type allow-list、ego center/depth/limit 校验与 BFS、去重无向 `link_count`、`source_refs` 摘要连边、overview 稳定排序/截断，以及 `familiar/familiar_count`
  - MCP 结构化工具结果与图片：`structuredContent` 追加到模型可见输出、等价 JSON 去重、`raw` 保留结构、摘要文本与结构化结果并存，以及 MIME/大小/数量校验后的图片 data URI 与 `[Image: mime]` 占位
  - MCP 端点目录与写入：12 个工具的目录/分组/默认只读集合、显式 enabled、工具去重与目录排序、名称/KB/Agent/限流校验、受限 API Key 的 KB/能力覆盖检查，以及隐藏 token_hash/deleted_at 的响应
  - MCP 公共端点运行时：`POST /mcp/{endpoint_id}` 无状态 Streamable HTTP JSON-RPC、Bearer-only 鉴权与 401 `WWW-Authenticate`、禁用端点/非活跃空间 403、`initialize`/notification/`ping`/`server/discover`/`tools/list`/`tools/call`、allowlist 隐藏与隐藏调用 `-32602`、端点级分钟限流、`last_used_at` 分钟节流，以及 12 个工具的 KB 作用域、Wiki、文档写入和 ask 会话链路
  - MCP `2026-07-28` 现代协议：`server/discover`/`supportedVersions`、现代结果 `resultType`/`_meta`/`cacheScope`/`ttlMs`、现代 GET/DELETE 仅允许 POST 且返回 405、旧版 DELETE 204、已移除方法 HTTP 404 + `-32601`、参数/请求错误 400；客户端优先发现现代协议，传输失败仅重试一次后回退 `initialize`、JSON-RPC `-32601` 不重试，递归读取 `x-mcp-header` 生成 `Mcp-Param-*`（SEP-2243 Base64 包装、JavaScript 安全整数限制），缺失工具目录时先完整拉取再调用
  - MCP 连接测试按能力读取工具/资源目录：未声明的能力失败时忽略并返回空列表，已声明的能力失败时测试失败；embedded resource 的 URI/MIME/文本与 `resource_link` 均进入模型输出
  - MCP `ask` 完整 Agent 环路：quick-answer 复用单次回答，`smart-reasoning` 复用聊天事件流执行工具循环并回填最终答案/引用；预创建用户/助手消息避免重复落库，4 分钟超时，非交互通道审批/OAuth 立即失败；`search_knowledge` 与 `knowledge_search` 配置名兼容
  - Agent SSE 事件契约：thinking 正文/done、tool_call content/iteration、tool_result success/error、审批/OAuth 与 steer 注入的 content/done、内部 error 终止事件、session_title 的 id/done/session_id，以及 complete 的 total_steps/total_duration_ms、data.usage 和顶层 usage；后端按模型轮次聚合真实 token usage，Agent 子任务与父流共享同一统计对象
  - 内置 Agent catalog：4 个用户可见 Agent 的顺序、完整配置与 tenant 替换，两个内部 Agent 的详情读取，落库内置 Agent 的 config 保留与复制，内部 Agent 列表隔离，自定义 Agent 响应字段，以及模型 provider 的 29 项目录、字段、顺序和本地化内容
  - Agent 编辑器 catalog：7 组提示词占位符、5 类类型预设的顺序/config/kb_filter、Accept-Language 与默认 locale 解析、英文 fallback，以及 false/空值字段的 Go omitempty 语义
  - 推荐问题：agent_config curated、starter 关闭、文档生成问题、列表 limit/count 语义、缺失 Agent 404、非法 tag_scopes 400、文档/FAQ 标签范围、无效标签不回退、KB 限定 API key 的显式与隐式范围，以及嵌套 questions 响应
  - IM 渠道 wire shape：Agent summary 精确字段与凭证隐藏、`credentials_configured` 真假、overview 字段/排序/内置 Agent 本地化/软删 Agent 过滤，以及 Create 仍返回原始凭证
  - IM 渠道 Create/Update 校验：缺失/非法 platform、非法 locale、不存在或越权 KB、不存在 Agent 均返回 400；更新失败不覆盖旧值，KB/locale 可清空，WeChat 强制 `longpoll`/`full`
  - WeCom 长连接：订阅帧与凭据、回调解析（群聊 @ 去除、voice、mixed image）、`aibot_respond_msg` 回复、停止关闭 socket、重连退避上限，以及运行时按 enabled/mode 启停渠道
  - WeCom 图片/文件：AES-256-CBC 解密、实际图片 MIME、文件扩展名探测与文本解析、附件提示词转义、消息附件持久化、视觉 data URI 入模和 IM 图片完整链路
  - 知识复制/移动后台 worker、取消后保持终态、`run_now` 恢复、新目标创建与重跑去重、未知任务与跨租户 404、请求兼容性/必填字段/文档归属与解析状态校验、移动双模式的 chunk ID/分块配置/标签与源 Wiki 清理/目标 Wiki 重建语义、共享 Wiki 页面来源剥离、批量重解析重建 chunks 并可检索、启动后孤儿任务恢复
  - 上传目录不再静态暴露；KB 文件按 KB 内容引用校验，消息文件按消息持久化引用/有效 artifact 校验
  - KB 绑定对象存储后的上传 PUT、预览 GET、复制共享引用保护和最后一个引用删除
  - KB copy worker 的源文件服务端 CopyObject、presigned GET/HEAD、篡改签名拒绝和对象 HEAD 句柄释放
  - KB 复制的 chunk 内嵌图片服务端复制到目标 knowledge 前缀、URL 去重、`original_url` 改写、父 chunk 正文/`source_content` 二次改写、父子/邻接 ID 重映射及 `chunk_images` 投影
  - S3/MinIO/COS/TOS/OSS/KS3/OBS 七类对象存储的 CopyObject 方法、CopySource 签名头和目标知识前缀
  - 复制/移动/重解析 worker 的跨实例原子认领、租约续期、失租停止和终态释放
  - 复制/移动/重解析 worker 的 3 次重试预算、10/40/90 秒退避、重试计数投影与 `run_now` 保留计数
  - FAQ 导入 worker 的跨实例认领、启动恢复、正式 5 次/dry-run 3 次重试预算与取消保护
  - 运行队列 retry 状态、next_process_at、pending/retry 分组计数及 retry 任务 run_now
  - 未过期 worker lease 在启动恢复时按到期时间延迟重新认领
  - `/duplicate` settings-only 语义、201 响应结构、生成画像/临时/置顶状态清理及目标内容为空
  - FAQ 知识库复制的标签 `seq_id` 映射、条目字段同步与重复执行幂等
  - 审批决议在本地缓存丢失后仍可从数据库完成
  - URL 知识导入的 loopback 阻断
  - 公网 URL 防护的地址分类、DNS 多地址、重定向链和大小/跳转限制
  - 系统用户列表分页：非法参数回退、`limit` 200 上限、`has_more` 与密码哈希隐藏
  - SQLite browser/model catalog 迁移脚本重复执行幂等
  - IM 出站 HTTP 交给 URL Guard、拒绝 SSRF 目标并保留 HTTP 状态错误
  - Anthropic 目录 `compat` 驱动 adaptive thinking/effort、Sonnet 5.5 `between_tools` 与 Opus 5.5 不可关闭 thinking
  - 初始化聊天连接检测：非鉴权 HTTP 400 返回可用，鉴权标记 400 与 401 仍失败
- 最近一次全量验证：干净临时数据库下 `444 passed, 7 skipped`；7 个 opt-in 用例已分别通过真实 Docker Engine（3）、MinIO（1）和 Redis limiter（3）验证
- `compileall -q app tests` 通过

### 上游提交审计（2026-10-18 续）

- `b5fd79edf` 聊天连接检测：`POST /initialization/remote/check` 不再把所有 HTTP 400 视为失败；非鉴权 400 返回 `available=true`，401 或带 `api_key_invalid`、`invalid api key`、`unauthorized`、`authentication` 的 400 仍失败。`model_runtime._http()` 改为保留上游状态码和响应体。
- `dad81cb8c` Docker idle sweep 与 exec 竞态：exec 返回非零后重新 inspect，只有 Engine 确认容器已消失才改判 `not_found`，消息保留 `exit=<code>`；传输错误或容器仍存活时保留原结果，避免误重建健康沙箱。
- `4243018ff` MCP 连接测试目录能力：工具和资源列表都会尝试读取；读取失败只在服务端声明了对应能力时判失败，否则记录为空列表继续。响应会保留另一类目录的成功结果。
- `172d0a85a` MCP embedded resource：工具结果中的嵌套 `resource`（文本或 blob）、扁平 resource 和 `resource_link` 都会生成可读引用；embedded 文本资源正文一并交给模型，blob 只保留 URI/MIME 引用。
- `9842e51d4` 模型复制配置：`POST /models/{id}/copy` 改为只接收必填 `display_name`，去除首尾空白并限制 255 个 Unicode 字符；副本保留源 name/type/source/description/parameters/credentials，嵌套参数深拷贝，强制 active、非默认、非内置，内置模型返回 400。响应不回显密钥，既有静态 UI 已调用该端点。新增端到端覆盖缺失/空白/超长展示名、内置与不存在模型、字段保留、嵌套参数和凭证脱敏。
- `5225edc0f` 注册模式：`registration_mode` 支持 `self_serve`/`invite_register`/`invite_only`，系统设置写入校验枚举，`/auth/config` 读取数据库实时值；公开注册只允许 `self_serve`，邀请注册允许 `self_serve`/`invite_register` 并返回 201，其他模式返回 403。新增即时切换并同时验证两个入口的端到端用例。
- `27b9ee74a` S3/COS 寻址：对象存储上传与连通性探测统一遵循 path-style 默认、AWS 与 `*.myqcloud.com` virtual-hosted style、`force_path_style` 优先，并避免 virtual-hosted URL 重复 bucket；`cos.myqcloud.com.example.org` 等相似域名保持 path-style。新增四种寻址组合及上传/探测 URL 一致性测试。
- `188d9026d` OpenRouter native thinking：legacy `thinking_control=none` 与 OpenRouter 组合不再提前返回，模型自身布尔 `thinking` 会写入 `reasoning.enabled`；非默认编码仍显式覆盖，未知值写入 `chat_template_kwargs`。新增 native reasoning 回归用例。
- `bccb4b151` 仅上传聊天：空 query 在内联图片或附件存在时放行，图片/附件先解析再持久化；用户消息保留空文本，历史回放按 locale 使用 `请根据我上传的内容回答。`/`Please answer based on what I uploaded.`。仅预上传 `attachment_ids` 仍按上游要求 query。
- `c9723b119` EPUB spine：OPF 解析优先采用 spine `itemref` 顺序，追加 manifest 中未列入 spine 的 XHTML 文档，并对重复引用去重；新增 spine/manifest 顺序与重复 idref 回归用例。
- `f46c99056` Host context 追问归因：Web/Embed 聊天入口在启动 SSE 前校验 `suggestion_attribution`。精确匹配建议文本，或识别 `[Host context]\n` 信封；信封内容必须非空、以空行结束，并从尾部锚定建议文本，因此 context value 可含换行/空行，但空信封、额外前缀、信封与问题间文本、问题后附加文本均返回 400。新增精确匹配、Host context 多行值、非法信封和入口 400 用例。
- `bcbd65a45` DingTalk Validate 韧性：Python 已有 workspace root 失败聚合、分页 listing 预算、最多 5 次文档探测、探测失败后继续、inconclusive folder 接受和探测上限提示；`tests/test_dingtalk.py` 已覆盖，无需改代码。
- `19542f2a1` Seafile 数据源：本轮实现 `app/services/seafile.py`、datasource 分派、资源祖先、同步文件名/类型透传、失败指纹保留和列表游标隐藏，并新增 fake api2/fileserver 与端到端测试；文本提取之外的图片/音频入库和真实租户联调仍保留为后续项。
- `d2bc0df6a` zh-TW locale：IM locale 白名单此前已含 `zh-TW`；本轮为 MCP usage-instruction 生成补上繁体中文分支，并保留 `zh-Hant*` 变体覆盖，新增直接回归测试。前端静态产物已包含 zh-TW 语言包，不重构建前端。
- `0de62b2f8` 引用来源导航：Python MCP `read_document` 已有 `chunk_ids` 与 `knowledge_base_id`，本轮补上 `source_chunk_id`（首个带 `source_locators` 的返回 chunk），并扩展现有 MCP 端到端测试验证结构与引用 ID；静态前端已消费该字段。
- `0c5d28abb` 多模态图片 embedding 索引/召回：Python 已有模型图片输入声明与 imageprep、image_vector 独立 chunk、caption/OCR 前置、增量重建/复制/移动、SQLite 图片向量召回和视觉上下文附加，现有 image-vector 测试覆盖这些契约，本轮不改代码。与 Go 的差异仍是 SQLite 预取 5000 行而非按引擎扩大候选池，以及外部向量库运行时尚未接入，已归入已知缺口。
- `676a59307`、`ae9054626` 为前端设置页布局/未保存提示，后端无对应执行路径；现有 `static/web` 产物已包含相关 UI，按前端-only 归档。
- `1c528d83d` 为 Seafile 的 zh-TW 前端 i18n 补充；静态产物已含 Seafile 资源类型、图标和文案，本轮后端接入即可，无需重构建前端。
- `b7f318106`、`a729d1fc8` 为文档-only 提交（模型供应商计数、rerank 限制说明），不影响 Python 运行时或 API，按文档项归档。
- `17dc7da51`、`d2dbf7209`、`a6327f8e3`、`769a5aeca`、`d8fa5b49a`、`50a296316`、`a58fc1320`、`bb878af50`、`6e2e5d8bb` 为前端 gallery、样式、i18n、Embed 截图粘贴、引用高亮/逐字展示和上传上限提示，均无新增后端契约；继续复用既有 `static/web` 产物，不重构建前端。
- `54cb6e9f9`、`def582b98` 仅为 Go docreader split 工具和 WeCom endpoint 测试补充/静态检查修复，Python 已有等价文本切分路径，不需要移植测试文件。
- `097bd475f`、`602d0c388` 仅调整 Go 应用镜像的 CA 安装顺序、构建下载镜像和 Compose 参数，属于部署构建项；Python 运行时不使用这些 Dockerfile/脚本。
- 已覆盖的运行时项：`03e2c47ae` MCP `2026-07-28` 现代协议与 `Mcp-Param-*` 已实现并有端到端测试；`0c71fff2a` 的 E2B `in_progress` 已在 `_normalize_template_status` 归一为 `building`，现有目录测试验证该状态；`8319d59dd` DingTalk 上传 docx/pptx/xlsx/pdf 的开关、dentry、预签名下载与文本提取已实现并有 fake API 测试。
- `b14bcbcd5` 为 Notion 正文抓取失败时保留同步游标；Python 尚未接入 Notion connector，按外部连接器缺口归档。
- 本轮 86 个未记录提交已完成处置，其中 10 项已有等价实现，其余按前端-only、测试/部署、文档或缺少对应子系统归档。UI-only、CLI/SDK/MCP-server、文档或部署镜像项无需移植：`9a2cd2006`、`4dd19b535`、`9d4cc233c`、`412ce63f0`、`75d2c1b58`；`49e262147`、`b413023d2`、`ff3318443`、`cefd5920f`、`7d75ce3a6`、`4a0c7fa91`、`4ea94afcb`、`562127c65`、`11bfacad9`、`69a7fd993`；`3206e15e8`、`dba912755`、`0c6b30cfa`、`176aa0d02`。
- 依赖尚未接入的外部运行时或连接器：`4eb23a856`、`afc6cbd50`、`5f5093b9f`、`f9eeb8dc8`、`1912ba26c`；Notion `dabbdaf15`、`5077f3580`、`402f69832`，GitLab `a2aeb97a0`、`1dbe9d0c1`，IMA `a6c6c35ad`、`b71b31c89`，Yuque `0d3a053eb`，Feishu `b5c74b2dd`；数据分析和 dataset 工具 `336f84c3b`、`a8c291c6f`、`f6119ae5f`、`0ec945fe5`。
- 当前 Python 组件面不存在对应子系统，因此这些修复不适用：`98feef38d` 表格转义竖线（无 Go header tracker/table-width 逻辑）、`e915adb14` 表格子块 source offset（无 parent/child offset 管线）、`e29301115`/`9490035f1` 图方向与截断（无 `query_knowledge_graph`/Neo4j 运行时）、`75fc0ad15`/`9de0946e5` AnyDoc（使用原生 zip/XML 提取）、`fac9ebabf` knowledge span、`fa4f85d3d` Agent compaction/checkpoint、`b8d83eab1` skill 安装脚本枚举；`52893006a` 的 Unicode 参数长度修复也因 Python 没有 Go 的通用 JSON-schema 参数校验器而不适用。
- 现有 Python 路径已经满足或天然规避：`626185a12` `_rebuild_links` 为事务性全成/全败；`a8a2c17b8`/`72b8a106c` SQLite 索引写入与主行同事务；`6057a8101` FastAPI access log 不缓存请求体；`28734725a` MCP 客户端按调用创建，无共享旧 session；`e394c7ade` 无 detached MCP 预热 goroutine；`20038cbda` 任务错误只落在内存/SQLite，不存在 PostgreSQL dead-letter 插入路径。
- `a15dfdd94` 坏 JSON 文档摘要：regenerate 与创建/重解析异步摘要均已接入 summary model，解析器拒绝截断/畸形 JSON，纯文本与首 chunk 兜底保留。
- 2026-10-18 收口记录：创建/单文档重解析/批量重解析已通过 `py_task_results` 的 `summary_generation` 任务异步生成摘要，复用 300 秒租约、60 秒续租、启动恢复和 3 次重试；`summary_enabled=false` 可跳过，模型不可用时直接发布首个文本 chunk 兜底，生成期间 chunk/元数据变化会丢弃过期结果。smoke 覆盖自动摘要、重解析重入队、模型不可用兜底和禁用开关。
- 2026-10-18 收口记录：`PUT /knowledge-bases/{id}` 已按上游拆分共享写权限。共享 `editor` 可更新名称、描述和普通配置；顶层 `vlm_config` 仅允许 KB 所属租户或共享 `admin`，其余共享角色返回 403 且不产生部分更新。跨租户更新在访问校验后按 KB ID 落库，smoke 覆盖共享 editor 元数据更新、VLM 拒绝和共享 admin 成功路径。
- 已覆盖、无需改动：`c3f077ed1` 模型配置与多模态 embedding（Qwen3-VL messages 批量、相似度调试、图片 embedding/rerank 均已有等价实现）；`de2dfd303` 远程图片 20 MiB 上限；`d84cdabac` RSS 指纹已含 title/link 并在元数据变化时刷新；`f55947349` MCP DCR `client_secret` 已持久化并使用；`c0c194f62` WeCom 混合图文已按首图和文本 caption 处理；`e7aaf8ae0` 空闲 HTML/docreader 在现有文本提取路径中不构成缺口。
- 已覆盖的后续审计：`150d8cc60` Docker 交互终端已有 Engine PTY；`1a21af1b8` MCP structuredContent 已透传；`3f9af2afa` MCP metadata 迁移已包含；`d272a83cf` 出站拨号超时已接入；`e8cc9c763` MCP 端点已包含共享 KB；`2ec177462` 登录/刷新 token 与事务同提交，查询错误不会被误报为撤销；`54e8d7ffd` 因 Python MCP 客户端按调用创建和关闭而不适用；`98e9dc078` MMR 已在 Python `retrieve` 截断前接入；`45a022aac`、`04da26024`、`9fa9e7c29`、`5648d24a9` 依赖尚未接入的外部向量库运行时。
- 2026-10-18 收口记录：Agent Chat 新增 `wiki_search`、`wiki_read_page`、`wiki_write_page`、`wiki_replace_text`、`wiki_rename_page`、`wiki_delete_page`、`wiki_flag_issue`、`wiki_read_issue`、`wiki_update_issue`。工具支持共享 KB 权限、链接重建、Agent 修订记录、source_refs 校验、issue 短 ID 解析和禁止手工创建 `summary/*`；只在 `allowed_tools` 显式包含工具名且存在启用 Wiki 的可访问 KB 时装配。
- `abc365f91` Wiki 表行缩减保护：新增 Markdown 数据行身份提取与集合比较，表头、分隔行、重排、强调符、大小写、全角空格和重复行合并不会误报。Agent 整页写入零容忍；同步 Wiki ingest 仅在丢失比例严格大于 20% 时拒绝。拒绝时页面正文、版本和修订记录均保持不变；人工编辑、精确替换、重命名、删除和回滚允许显式缩减。新增纯函数算法测试及 Agent/ingest 集成回归。
- 当前功能面不适用：`52617f641` Aliyun E2B snapshot（Python 无 skill snapshot/`FromSnapshot` 流程）；`a75e8e9ac` Yuque/Notion/IMA/Feishu 响应上限（连接器未实现）；`e6ecb9adc` dataset QA 模型选择（无 `dataset/` 工具）；`8cb53fdbc` Langfuse 诊断 span（无对应追踪层）；`b5c8d90cc` 图抽取拒绝跳过（无异步图抽取任务）；`7c284e7fc` FAQ chunk 状态同步（Python FAQ 直接存 `py_faq_entries`）；`4d7e66312` Wiki 异步 ingest pending op（Python 为同步 ingest）。
- `cbfd66fd9` 存储配额收口：新增 `app/services/storage_quota.py`，创建/克隆/重解析/数据源更新在写 chunk 前按增量原子预占 `tenants.storage_used`；超限创建保留 failed/disabled 知识行但不写 active chunk，手工更新/重解析/克隆事务回滚且释放已复制文件。删除知识、批量删除、KB 删除和数据源删除对账统一按 `storage_size` 释放。smoke 覆盖超限零 chunk、恢复后重试成功、手工更新回滚、克隆回滚和数据源更新保留旧 chunk。
- `a1450d382` 资源目录收口：新增 `app/services/resource_catalog.py`，按上游 22 位 handle 规则扫描正文/`image_info` 中的 `resource://`，为知识条目建立幂等 attachment/extracted_image binding；克隆后重新 claim，重解析前只释放 `ImageInfo` 图片，知识真正删除时取 `ImageInfo ∪ owner bindings`，仅最后一个 owner 释放后删除字节并把 resource 标记 deleted。Python 直传本地/对象存储仍使用既有 provider 引用，不额外注册 catalog handle。

## 已知缺口与待办

按优先级从高到低：

1. **BrowserSkill 收尾**
   - 多副本 lease 路由、签名内部 RPC 与防重放已完成。
   - 核心失败语义已与 Go 对齐；仍需持续核对新增 daemon 错误码与恢复提示。
   - 协议级联合测试已覆盖握手、双向帧、事件暂停、UI 隔离、断线释放和超时 cancel；真实原生扩展/daemon/Chromium 联合测试仍需 `BROWSERSKILL_TEST_BINARY` 与 Playwright/扩展环境。

2. **Docker/Cube/E2B 后续能力**
   - 管理端 deep check 已完成创建、执行、egress 探测和销毁。
   - 模板目录、标准/桌面模板的 ensure/replace 构建和替换后配置回写已完成；Docker 会话容器、exec、TTY Exec+resize、stop/unpause 恢复、idle sweep、模板目录和后台 pull 失败状态已用本地 daemon/镜像真实验证。成功拉取远程镜像仍待可访问 registry。
   - 会话级 Docker/Cube/E2B 远程命令、真实 PTY terminal WS 和 desktop RFB 中继已接入；terminal 已支持原始键盘输入、resize、Cube/E2B PID 重连、Docker 进程树清理、周期鉴权和每会话 5 个 PTY 上限。该上限与上游一致，明确为单进程计数器；仍需真实 Cube/E2B 云端 PTY/desktop 联调。

3. **剩余占位/投影接口审计**
   - 运行时队列已支持 SQLite 任务表查询、取消、删除、归档清理和归档 FAQ `run_now`；仍缺 Redis/asynq 多队列与通用任务重跑。模型并发限流已在配置 `REDIS_ADDR` 时使用跨进程 Redis lease，未配置时保持进程内信号量。
   - FAQ、复制/移动/重解析均已接入后台 worker、取消、`run_now`、启动后孤儿任务恢复、SQLite 跨进程 worker 租约和类型特定重试/指数退避；仍需对照 Go 的 Redis/asynq 跨进程队列、调度状态和更细的中断恢复。
   - 向量库注册/CRUD 元数据已与上游对齐，但实际检索运行时仍只有 SQLite；外部向量库尚未接入。
   - 图片多模态向量已接入 Markdown/HTML 引用、docx/pptx/PDF 内嵌图片、直接图片上传、重解析/移动/重建索引与 SQLite 检索，embedding/rerank 前也已完成上游 `imageprep` 的格式转换/缩放；VLM caption/OCR 与音频 ASR 已接入，视频按上游明确拒绝。
   - 文件代理已支持本地与对象存储读取；知识上传、预览/下载回读、幂等删除、KB 复制源文件与 chunk 内嵌图片服务端 CopyObject、presigned GET/HEAD 已接入。MinIO 的 PUT/GET/Copy/Delete 已通过真实临时 bucket 验证（`tests/test_object_storage_integration.py`，opt-in）；S3/COS/TOS/OSS/KS3/OBS 仍为签名单测与 mock 闭环，尚未用真实账号联调。
   - Web 搜索 14 个 provider、保存时校验、代理、数据库/环境变量 SSRF 白名单和固定 IP 请求器已补齐；仍需真实私有 SearXNG 与真实代理联调。

4. **完整 parity 审计**
   - 对照 Go 源码逐模块核验错误码、权限边界、分页、SSE 事件、任务取消和异常恢复。
   - 对前端实际调用路径继续做静态与运行时核对。
   - 存储配额/chunk 清理与 `resource://` 目录 claim/release 已接入；`dataset` QA 工具仍是已确认缺口。
   - MCP 公共端点已可连接并覆盖 12 个工具；`ask` 已支持 quick-answer 单次生成与 `smart-reasoning` 完整工具循环。现代/旧版协议、发现结果字段、参数头、错误状态码、瞬态 discovery 重试、现代 GET/DELETE 语义和 Agent SSE 事件字段均已对齐。
   - DingTalk datasource 在线文档与上传 Office/PDF 已接入（分页、资源选择、adoc 渲染、钉盘预签名下载、纯文本解析、元数据透传、Validate 采样边界、增量指纹与删除回收）；上传文件内嵌图片保留和真实租户联调仍待补齐。
   - WeCom 长连接已接入文本/语音、图片/文件 AES-256-CBC 下载、文本解析、消息附件持久化、视觉/文档入模，以及渠道知识库后台入库与哈希去重；视频仍明确回复不支持。真实企业微信凭据、真实临时下载 URL 与真实 Office/PDF 媒体样本联调仍待执行。
   - WeCom 长连接已用共享 SQLite 实现 15 秒跨进程租约与自动接管；副本必须共享同一数据库。若采用独立数据库部署，仍需补 Redis leader 选举。

## 恢复工作时的验证命令

```bash
cd /home/PyWeKnora
/home/ikc-log-center/.venv/bin/python -m pytest -q
/home/ikc-log-center/.venv/bin/python -m compileall -q app tests
git remote -v
```

## 备注

- 本文件只记录工程状态，不代表目标已完成。
- 目标仍是完整复刻；下一次应优先推进 BrowserSkill 收尾或 Cube/E2B 真实运行时。
