# PyWeKnora Agent 指南

更新时间：2026-10-18  
用途：给后续编码 Agent 提供项目边界、当前进展、验证方式和下一步优先级。详细变更记录见 `process.md`，API 源定义见 `docs/swagger.json` 与 `docs/swagger.yaml`。

## 项目目标

用 Python/FastAPI 复刻 `/home/sharkyai/WeKnora`，复用既有 Vue 构建产物 `static/web/`，不重新构建前端。判断是否完成时，以 Go 实现的 API、权限边界、异步任务和前端实际调用路径为准。

## 当前状态

- 后端主体已落地：认证、租户/组织、知识库/文档/分块、检索、聊天/SSE、Agent、MCP、记忆、Wiki、FAQ、嵌入渠道、模型、存储、系统管理和 IM 渠道。
- FAQ 批量导入已异步化：`POST /knowledge-bases/{id}/faq/entries` 创建 `faq_import` 任务，后台校验/分批导入并持续更新进度，支持 `dry_run` 与 `replace`。FAQ worker 使用与知识 worker 相同的 SQLite 原子认领、300 秒租约、60 秒续租和启动恢复；正式导入有 5 次重试预算，dry-run 为 3 次。FAQ 知识库复制会按标准问题幂等同步 `py_faq_entries`，并复制同名标签、将源 `tag_id`（`seq_id`）映射到目标标签。
- 运行时队列管理已接入 `py_task_results` 投影：支持 pending/active/scheduled/retry/archived/completed 状态、队列统计、分页查询、取消/删除任务和清理归档；重试中的任务返回 `next_process_at` 与真实的 `retried/max_retry`，并支持立即执行或取消。归档任务支持 `run_now` 重新调度，worker 会遵守取消，不再覆盖终态或继续写入。
- 工具审批与 MCP OAuth 状态已从进程内字典迁到共享数据库表；同进程保留 Future 快速唤醒，跨实例通过数据库轮询决议。
- MCP 结构化工具结果已透传给模型：`structuredContent` 会以 `Structured content:` JSON 追加到工具输出；若文本内容已经是等价的序列化 JSON 则不重复，`raw` 始终保留原始结构，错误结果也保留结构化详情。MCP 图片按 PNG/JPEG/GIF/WebP、10 MiB 解码大小和最多 5 张的限制转成 data URI 交给视觉模型，非视觉模型会看到 `[Image: mime]` 占位提示。
- MCP 端点管理已对齐：工具目录返回 12 个工具、4 个分组和 `default_tools`；创建默认只勾选 9 个只读工具，保留显式 `enabled:false`，未知/重复工具按目录顺序归一化，名称、KB、默认 Agent 和 0–6000 的限流值在写入前校验；响应隐藏 `token_hash/deleted_at` 并返回布尔 `enabled`。受限 API Key 创建/更新/轮换端点时必须被其 KB allow-list 和 retrieve/chat/read_agents/ingest 能力完全覆盖。
- MCP 服务创建同样保留 `enabled` 三态语义：字段缺省时默认启用，显式 `enabled:false` 不会被默认值覆盖；列表、创建和单资源响应统一返回布尔 `enabled`，并新增回归测试覆盖创建、读取与列表。
- MCP 公共端点已从管理 API 补齐到可连接运行时：`POST /mcp/{endpoint_id}` 提供无状态 Streamable HTTP JSON-RPC（`initialize`、`notifications/initialized`、`ping`、`server/discover`、`tools/list`、`tools/call`），仅接受 `Authorization: Bearer mcp_...`；缺失/无效 token 返回 401 + `WWW-Authenticate`，禁用端点或非活跃空间返回 403。工具列表按端点 allowlist 过滤，隐藏工具调用返回 `-32602`；每个端点独立分钟限流，`last_used_at` 最多每分钟更新一次。12 个工具均已接入现有 SQLite、检索、Wiki 和知识写入链路，并覆盖 KB 作用域、文档读写和 ask 会话隔离。`ask` 已复用聊天/Agent 事件流：quick-answer 走单次生成，`smart-reasoning` 走完整工具循环，4 分钟超时；MCP 无人工交互时审批类工具和 OAuth 会立即失败而不是挂起。
- MCP 现代协议已支持 `2026-07-28` 与旧版 `2025-11-25` 并存：服务端实现 `server/discover`/`supportedVersions`，现代结果补齐 `resultType`、`_meta` 和列表/发现所需的 `cacheScope`/`ttlMs`；现代 GET/DELETE 返回 405 且仅允许 POST，旧版 DELETE 保持 204；现代已移除方法返回 HTTP 404 + `-32601`，参数/请求错误映射为 400。客户端优先发现现代协议并回退 `initialize`；现代 `tools/call` 会按工具 `inputSchema` 的 `x-mcp-header` 递归生成 `Mcp-Param-*` 头，字符串按 SEP-2243 做 Base64 包装，整数限制在 JavaScript 安全范围内，缺失工具目录时先完整拉取再执行。
- MCP 连接测试已按 `initialize` 公布的能力处理目录读取：工具或资源列表读取失败时，只有对应能力被声明才让测试失败，否则按空列表继续；未声明能力的目录仍会尝试读取。工具结果中的 embedded resource 会保留 URI/MIME 并附上嵌入文本，`resource_link` 以引用形式传给模型。
- Agent SSE 事件字段已按 Go 流处理器对齐：`thinking` 正文进入 `content` 并补 done 标记；`tool_call` 使用 `Calling tool: ...` 且携带 iteration；`tool_result` 透传 success/error/duration；审批/OAuth 与 steer 注入的终端事件使用正确的 content/done；内部异常改为 `error` 事件而不是中断流；`complete` 返回真实 `total_steps`、`total_duration_ms` 和跨工具轮次聚合的 usage（同时放在 `data.usage` 与顶层 `usage`）。会话标题事件补 `id`、`done` 和 `session_id`。
- 初始化聊天连接检测已对齐上游 `b5fd79edf`：`POST /initialization/remote/check` 对非鉴权类 HTTP 400（如模型参数或请求格式错误）返回可用；仅 401 或响应包含 `api_key_invalid`、`invalid api key`、`unauthorized`、`authentication` 等凭据标记时判失败。`model_runtime` 的 HTTP 错误保留上游状态码和响应体，连接检测不再把非鉴权 400 误报为失败。
- Docker idle sweep 与 exec 的竞态已对齐上游 `dad81cb8c`：Docker exec 非零退出后会重新 inspect；仅在 Engine 确认容器已消失时改判 `not_found` 并在消息中保留退出码，真实命令失败仍按原结果返回，使后续 `ensure` 能清理失效绑定并重建沙箱。
- 另一批上游提交已核验为已覆盖或不适用：`c3f077ed1` 模型配置/多模态 embedding、`de2dfd303` 远程图片上限、`d84cdabac` RSS 指纹、`f55947349` MCP DCR client secret、`c0c194f62` WeCom 图文混排均已存在等价实现；`52617f641` Aliyun E2B snapshot、`a75e8e9ac` Yuque/Notion/IMA/Feishu 响应上限、`e6ecb9adc` dataset QA 模型选择因 Python 尚无对应功能不适用。`cbfd66fd9` 已补齐租户存储配额预占/增量调整/删除释放和超限零 chunk；`e7aaf8ae0` 空白 HTML/docreader 在现有文本解析路径中不构成缺口。
- Agent Chat 已接入 9 个 Wiki 工具：搜索、读取、整页写入、精确替换、重命名、删除及 issue 读取/标记/更新。写工具要求同租户 KB 创建者/Admin/Owner，或共享 editor/admin；只读工具允许 reviewer。`abc365f91` 的表行缩减保护同时接入 Agent 整页写入（零容忍）与同步 Wiki ingest（丢失比例严格大于 20% 才拒绝），拒绝时不修改页面或递增版本；人工编辑、精确替换、重命名、删除和回滚属于显式缩减，不受保护拦截。
- Agent 工具选择兼容目录配置名 `search_knowledge` 与运行时工具名 `knowledge_search`，内置智能推理不再因命名差异丢失知识检索工具。
- 系统用户管理已对齐上游：`GET /system/admin/users` 返回 `users/offset/limit/has_more` 且不暴露密码哈希，非法分页参数回退默认值、`limit` 上限 200；`POST /system/admin/users/status` 校验布尔状态、拒绝自禁用和禁用最后活跃系统管理员，禁用时原子撤销全部 token，`parse_auth` 每次请求复查 `is_active`，重新启用不会恢复旧会话。
- 迁移容错已对齐上游：SQLite 的 browser authorization、memory consistency、model catalog 表/索引定义使用 `IF NOT EXISTS`，model catalog 默认行使用 `INSERT OR IGNORE`，避免中断后重放已有对象时失败；包含 `ALTER TABLE` 的脚本仍依赖迁移记录只执行一次。
- IM 出站 HTTP 已统一走 `url_guard.request_url` 的固定 IP SSRF 校验，白名单仍可用于私有部署；因此自定义 `api_base_url`、Mattermost `site_url` 等不会绕过现有 URL 防护。出站连接建立默认 5 秒超时，可用 `WEKNORA_OUTBOUND_DIAL_TIMEOUT`（Go duration，如 `250ms`/`10s`）调整；该上限只收窄拨号阶段，不改变响应读取的请求超时。
- Claude 模型目录已补齐 `compat`：Opus/ Fable/Sonnet 5 系列使用 adaptive thinking 与 effort，Sonnet 5.5 关闭 thinking 时发送 `between_tools`，不支持关闭的 Opus 5.5 不再发送 thinking 字段；目录解析出的兼容配置会直接驱动运行时请求体。
- 权限边界已补齐一批缺口：系统/配置只读端点要求登录；sandbox 配置、skills catalog、模型、存储、向量库、web-search、datasource、MCP 端点/服务及相关测试/同步/凭据写操作要求 Admin；KB、文档、分块、生成问题、标签、FAQ、Wiki 和初始化配置写操作要求 KB 创建者或 Admin；知识文件下载要求 Contributor+；组织写操作要求 Admin，租户成员/邀请/API key 管理要求 Owner；Agent 创建/复制要求 Contributor，更新/删除要求创建者或 Admin，IM 渠道管理要求 Admin；evaluation 创建要求 Admin；附件、message artifacts 和 suggestions 均校验会话所有者。
- 会话管理权限已对齐频道场景：租户 `admin`/`owner` 可在 Web 控制台更新、单删和批量删除同租户的 IM、Embed、MCP 及带频道 owner 前缀的会话；其他用户的普通 Web 会话仍返回 404，跨租户和受限 API Key 也不得借管理员路径越权。批量删除按实际会话 owner 清理浏览器作用域；请求全部不可见时返回 404，混合请求只删除可见项。
- 检索上下文已补齐截断标注：`retrieve` 在保留前 N 条时同时记录候选池大小，聊天上下文用 `<subset shown="N" candidates="M">` 提醒模型不要在计数或穷举问题中把子集当全集；知识 ID 过滤下推到 SQL，避免先截断后过滤导致候选数失真。检索结果在截断前按 `MMR_LAMBDA=0.7` 做 MMR，使用清洗后的正文、图片 caption/OCR 和生成问题计算 token 集合 Jaccard 冗余。聊天引用同时保留知识文件名、来源、通道、描述、稳定排序的自定义元数据文本、chunk 序号和父 chunk。
- 文档摘要已接入创建与重解析：`_create_knowledge`、单文档重解析和批量重解析会按 `summary_enabled`（默认开启）创建 `summary_generation` 后台任务，使用 `py_task_results` 的 SQLite 租约、续租、启动恢复和 3 次重试；任务开始时置 `processing`，成功后写 `description` 并置 `completed`，失败终态回退首个文本 chunk 并置 `failed`。摘要输入按 24000 字符头尾采样，提示词要求结构化 JSON，解析器兼容 `summary`/`gist`、BOM 和 markdown 围栏；被 `length`/`max_tokens` 截断或畸形 JSON 不会落库为摘要。模型不可用时直接发布本地兜底，内容或自定义元数据在生成期间变化时丢弃过期结果。
- 知识文件下载在租户 Contributor+ 之外还要求 KB 写权限：同租户仍为创建者或 Admin，组织共享需要 editor/admin；共享 Viewer 不能下载源文件。
- 跨租户资源隔离已覆盖子资源更新与引用：标签、FAQ、API key 和 IM 渠道按 `tenant_id`（标签/FAQ 同时按 `knowledge_base_id`）更新并在作用域内回查，提交他租户 ID 统一返回 404，不返回或修改目标数据；文档标签和 FAQ 标签必须属于同一 KB。
- IM 渠道 Create/Update 已按 Go 校验请求：仅接受上游 10 个 platform 和 6 个 locale；非空 `knowledge_base_id` 必须属于当前租户且满足 API key allow-list，`agent_id` 必须存在；失败统一 400，更新被拒时不覆盖旧值。Create 返回 200 且保留空名称，WeChat 渠道强制 `longpoll/full`。
- WeCom `websocket` 长连接已落地：lifespan 启动/停止启用渠道，CRUD 后热同步；`aibot_subscribe` 认证、30 秒 heartbeat、90 秒读超时、1–30 秒指数重连、`aibot_respond_msg` 最终回复和自定义 `wss` 端点 SSRF 校验均已接入。HTTP callback 与长连接共用会话、去重和 Agent 事件处理。
- WeCom 长连接已加入跨进程频道租约：迁移 `000042_im_channel_leases` 提供 15 秒 SQLite lease、5 秒续租和 10 秒接管轮询；同一数据库上的多 worker/多副本只会有一个进程持有订阅，失租会停止适配器并自动重试接管。
- WeCom 图片/文件消息已接入问答：按消息 `aeskey` 做 AES-256-CBC/PKCS#7 解密，下载经 URL Guard 固定 IP 校验并限制 32 MiB；文件按扩展名/文件头解析文本，图片按实际 MIME 生成不超过 8 MiB 的视觉 data URI。附件元数据写入用户消息 `attachments`，解析文本以不可信资料块注入，视觉模型直接读取图片；渠道配置 `knowledge_base_id` 时，下载字节会在后台复用知识上传链路入库，按文件哈希去重，入库失败不影响问答回复；视频仍明确回复不支持。Yunzhijia 附件下载在 TCP/读取超时时会间隔 500ms 重试一次，父级失败仍原样返回，其他平台不额外重试。
- DingTalk datasource 已补齐在线知识库文档链路：`client_id/client_secret/operator_id` 凭证、access token 缓存与 401 刷新、workspace/node/block 分页、`dingtalk:v1?workspace=...&node=...&ancestor=...` 资源 ID、资源选择器、adoc 块渲染、条目元数据透传和完整扫描后的删除回收均已接入。Validate 已对齐 listing 预算、5 次探测上限、工作区失败聚合和 inconclusive 接受语义。开启 `include_uploaded_files` 后，上传 docx/pptx/xlsx/pdf 会经钉盘 dentry/预签名下载并提取纯文本入库；内嵌图片不保留。
- Seafile datasource 已接入 api2 链路：`base_url + api_token` 凭证、`<repo_uuid>:<absolute path>` 资源 ID、单库/根路径处理、资源祖先、库/目录/文件懒加载、递归扫描和完整扫描后的删除对账均已落地。文件下载先请求带 `Token` 的平台链接，再从 fileserver 无凭据读取；复用 URL Guard，并用 `oid/mtime/size` 指纹做增量跳过，失败文件保留指纹待重试。游标持久化 `repo_id/repo_name`，空库也能阻止切换库；`sync_mode=full` 会绕过连接器指纹跳过并强制重写已有知识。同步管线同时透传 `file_name/file_type`、保留失败项指纹并隐藏列表响应中的 `last_sync_cursor`。
- 组织共享 KB 的读取已覆盖标签、FAQ 和初始化配置；初始化配置按源 KB 租户解析模型，并对共享接收方隐藏 `baseUrl` 等集成细节。
- API Key 请求认证已接入：Bearer 无效时可回退到 `X-API-Key`；租户 Key 固定所属空间，平台 Key 在租户路由必须携带 `X-Tenant-ID`；撤销/过期 Key 返回 401。路由按 capability 默认拒绝，全量 Key 不受细粒度能力限制，平台控制面仅接受平台 Key；KB allow-list 已覆盖路径参数、知识库列表/搜索与聊天会话配置。
- API Key 路由策略已按上游矩阵复核：`/files` 仅允许全量 Key 或非 KB 限定的 `retrieve` Key；`/wechat` 归入 `manage_channels`；`/web-search/providers`、`POST /system/host-project-dir`、工具审批、个人收藏等交互/个人路由保持默认拒绝。
- Bearer access token 现在每次请求都会校验 `auth_tokens` 行存在且未撤销，与 Go 一致；用户禁用、密码重置或登出后旧 token 立即失效。系统用户状态接口已对齐必填字段、缺失用户 404、自禁用/最后管理员 400、脱敏 `user` 响应和禁用时即时撤销会话。
- 模型 catalog 解析已对齐：`/models/catalog/resolve` 精确匹配目录条目并返回 `cataloged:true`、真实 API、上下文/输出上限、输入类型、thinking levels 和 capabilities；Claude 模型选择原生 `anthropic-messages` 传输，支持文本/系统消息、Base64/远程图片、工具定义与 `tool_use`/`tool_result`、OpenAI 形状响应转换、`x-api-key`/`anthropic-version`，并对拒绝采样参数的模型省略 `temperature`/`top_p`。thinking 已支持 budget/adaptive 请求、level clamp、`output_config.effort`、thinking 与 max_tokens 预算协调，以及带签名的 thinking/redacted_thinking block 在工具循环内原样回放；当前 Messages 传输仍为同步非流式。
- 模型复制已对齐上游 `9842e51d4`：`POST /models/{id}/copy` 只接收必填 `display_name`，去除首尾空白并限制 255 个 Unicode 字符；副本保留源模型的 name/type/source/description/parameters/credentials，嵌套参数深拷贝，强制为 active、非默认、非内置，内置模型返回 400，响应继续对密钥脱敏。既有静态 UI 已直接调用该端点。
- 注册模式已对齐上游 `5225edc0f`：系统设置支持 `self_serve`、`invite_register`、`invite_only` 三态并即时生效；公开注册只在 `self_serve` 开放，邀请注册在 `self_serve`/`invite_register` 开放，其余返回 403；`/auth/config` 返回实时设置，邀请注册成功返回 201。
- S3 兼容存储寻址已对齐上游 `27b9ee74a`：上传与连通性探测统一使用 path-style，AWS `*.amazonaws.com` 与腾讯云 COS `*.myqcloud.com` 使用 virtual-hosted style，`force_path_style` 优先；相似域名不会被误判为云厂商主机。
- OpenRouter legacy thinking 已对齐上游 `188d9026d`：`thinking_control=none` 不再抑制模型自身的 `reasoning.enabled`，非默认 legacy 编码仍作为显式覆盖，未知值继续映射为 `chat_template_kwargs`。
- 仅上传聊天已对齐上游 `bccb4b151`：query 为空但带内联图片或附件时允许发送；用户消息保留真实空文本并保存图片/附件，后续历史以中英文“请根据我上传的内容回答”回放；仅传 `attachment_ids` 仍要求 query。
- EPUB 章节顺序已对齐上游 `c9723b119`：优先按 OPF spine 读取，追加 manifest 中存在但 spine 缺失的文档，并对重复 `idref` 去重。
- 追问归因已对齐上游 `f46c99056`：聊天入口在 SSE 启动前校验 `suggestion_attribution` 的 set/question、ready 状态和问题文本；Host context 信封按建议文本从尾部锚定，允许 context 内含换行/空行，但拒绝空信封、额外前缀或问题后的附加文本。Web 与 Embed 入口均返回 400 `invalid suggestion attribution`。
- 知识库复制、知识移动与批量重解析已改为后台 worker：创建时落 `pending` 并立即返回，worker 按条更新 `processing/completed/failed`；取消后不会覆盖终态，归档任务可用 `run_now` 重跑。复制会记录已完成源文档、移动会按当前目标 KB 去重，支持中断后继续。worker 通过 SQLite 原子认领任务，持有 300 秒租约并每 60 秒续租；多实例不会重复执行同一任务，失租会停止当前 worker，启动恢复会等待未过期 lease 到期后再认领。三类 worker 均有 3 次重试预算，失败后按 10/40/90 秒退避，运行队列投影真实 `retried/max_retry`。
- 知识移动按 `mode` 分流：`reuse_vectors` 保留原 chunk ID 并只迁移 KB 归属；`reparse` 保留知识 ID/源文件路径，按目标 KB 的 `chunking_config` 替换旧 chunks 并更新目标嵌入模型。两种模式都会删除 KB 作用域标签关系、清理源 KB Wiki 引用，并在目标 KB 启用 Wiki 时重建文档页面；仅属于该文档的源页面会软删除，共享页面只移除对应来源和 chunk 引用。
- KB 内容复制（后台 `/knowledge-bases/copy`）会把源文件复制到新知识 ID 名下，生成独立 `file_path`；对象存储使用服务端 CopyObject，本地存储写入新的租户/知识目录。同时深拷贝 chunk `image_info` 中的内嵌图片到目标知识自有前缀，去重相同 URL，并在全部 chunk 处理完后统一改写正文与 `source_content` 的 Markdown 图片引用；新 chunk 的 `pre/next/parent_chunk_id` 按源→目标 ID 重映射。移动保留原知识 ID，因此继续使用原路径。
- `POST /knowledge-bases/{id}/duplicate` 已对齐上游：返回 201，只复制 KB 设置并清空生成画像、临时/置顶状态和内容计数，不复制文档、chunks、FAQ、索引、分享或置顶，响应包含 `source_id/target_id/message/knowledge_base`。
- 批量重解析会优先读取源文件重新抽取，无源文件时使用现有 chunks 正文重新分块；成功后替换旧 chunks 并触发 Wiki ingest，无法抽取的文档单独标记 failed。应用启动时会扫描 `pending/processing` 的复制、移动和重解析任务并重新调度。
- Wiki 图谱接口已按上游 `231a5c0b1` 对齐：默认排除 index 导航页，支持类型过滤、ego BFS（center/depth/limit 校验）和 overview 截断；`link_count` 按去重后的无向可见邻居计算，互链只计一次，失效/自环/过滤链接不计入；文档摘要通过 `source_refs` 与同文档页面自动连边，overview 按连接度、熟悉文档、页面类型、更新时间和 slug 稳定排序，并返回 `familiar/familiar_count`。
- Steering 的 inject 持久化已按上游 `932b750b2` 对齐：轮次边界不再先消费事件，而是在同一事务内 CAS 标记并插入用户消息；写入失败会回滚并保留 queued，批次立即停止以避免后续消息越过前一条。同一条消息连续 3 次落库失败后会标记为 `dropped`，让后续消息继续；成功消息保留 `user_message_id` 与 `mentioned_items`，事件只在提交后发送。
- 复制/移动在请求入口和 worker 执行前都会校验 source/target 不同、KB 类型/嵌入模型/向量库/存储实例兼容；移动还强制 `source_kb_id`、`target_kb_id`、`mode`，并要求知识属于 source 且解析完成。
- 复制/移动进度按 `tenant_id + kind + task_id` 查询，未知或跨租户任务统一 404。
- 文件访问已收口：上传目录不再通过静态挂载暴露；KB 文件代理要求引用绑定到该 KB 的文档/分块/Wiki 内容，消息文件代理要求引用出现在该消息的持久化渲染字段或有效 artifact 中。`/files` 及 KB/消息/embed 文件代理现可读取租户对象存储：支持 `storage://<backend>/<provider>://...`、S3/MinIO/COS/TOS/OSS/KS3/OBS 签名 GET、流式响应和存储实例 provider/bucket 校验；本地与 `resource://` 引用保持兼容。知识上传会按 KB 绑定的存储实例（未绑定则按租户默认/legacy provider）写入对象存储，解析、预览、单文件下载和批量下载可回读；删除仅在最后一个活跃文档引用消失后清理对象。匿名 IM 取图入口 `/api/v1/files/presigned` 已实现 GET/HEAD：使用 `SYSTEM_SIGNING_KEY`/`SYSTEM_AES_KEY` 的 HMAC-SHA256、绑定 file_path/tenant_id/expires，同时支持本地和对象存储。
- 向量库注册与 CRUD 已按上游契约对齐：类型目录覆盖 Elasticsearch、Qdrant、Milvus、Tencent VectorDB、Weaviate、Doris、OpenSearch，SQLite/PostgreSQL 仅作为 env store；列表先返回 `RETRIEVE_DRIVER` 派生的脱敏只读 env store，再返回当前租户的 DB store，`GET` 与 `/test` 支持 `__env_*` ID。创建返回 201，校验可注册引擎、必填连接字段、索引配置、SSRF、endpoint+index 重复（含 env store）和连通性，并回写检测到的版本；更新仅允许改名，删除在仍有 KB 绑定时拒绝，env store 的更新/删除统一返回 400。Python 实际检索运行时仍为 SQLite。
- 网络搜索 Provider 注册元数据已对齐上游：`GET /web-search-providers/types` 返回 14 个 provider 的 `id/name/requires_api_key/requires_engine_id/requires_base_url/supports_proxy/config_fields` 等完整字段；14 个已注册 provider 均有运行时适配器，创建/更新时会校验 provider 类型、必填密钥、provider 专属选项、SearXNG URL 和代理 URL。
- 网络搜索出站请求已接入 SSRF 校验、重定向校验、显式 `proxy_url` 和数据库设置 `ssrf.whitelist` + `SSRF_WHITELIST`/`SSRF_WHITELIST_EXTRA`（支持精确主机、`*.domain`、IP 与 CIDR）；无代理请求会固定已校验 IP，SearXNG 私网实例只有命中白名单后才允许访问。
- 模型并发限流已接入：后台复制/移动/重解析 worker 标记为后台上下文，chat/embedding 调用按模型 ID 获取槽位；模型自身 `max_concurrency` 优先，其次读取 `model.max_concurrency` / `WEKNORA_MODEL_MAX_CONCURRENCY`（默认 32）。未配置 `REDIS_ADDR` 时保持进程内信号量；配置后使用 Redis ZSET lease（30 秒租约、200 毫秒轮询、心跳续租、崩溃后自动回收、后端错误 fail-open），`active` 为集群范围，`waiting` 为进程本地。`GET /system/admin/runtime/queues` 返回真实 `model_limiter_available` 与 active/waiting/limit 统计。
- 模型调试 `/models/{id}/debug` 已补齐多模态 Embedding/Rerank：文本侧按 Ark、DashScope、Gemini 原生协议编码；图片 embedding 覆盖 5 类请求形态，图片相似度保留“文档在前、图片在后”的请求索引并拒绝维度不一致，图片 rerank 覆盖 Jina/Cohere 与 NVIDIA NIM。文本 rerank 的 Hugging Face TEI 使用原生 `query/texts` 并读取裸数组，Pinecone Inference 使用对象 documents、`rank_fields`/`top_n` 和 `data[]`，分别按 32/100 条分批、回填全局索引，并校验响应数量、索引、分数与重复项。文本+图片混用、非图片文件和不支持图片的模型返回明确错误，模型 `capabilities.input` 同步返回 `text/image`。
- 图片多模态向量已接入生产 chunk 链路：KB 需同时开启 `indexing_strategy.vector_enabled` 与 `image_processing_config.image_vector_enabled`；Markdown/HTML 图片引用、docx/pptx/PDF/EPUB 内嵌图片和直接上传图片会生成 `image` 块，索引时调用图片 embedding，写入 `image_vector` 子块（`parent_chunk_id` 指向所属文本/图片块，内容优先 caption、缺失回退 OCR，直接图片无 caption/OCR 时使用文件名），向量持久化到 `chunks.embedding`。docx/pptx 解析按文内顺序映射关系表，优先使用 `docPr`/`cNvPr` 的 `descr`、`title`、`name` 作为 caption；PDF 使用 pypdf 按页提取文本与图片 XObject，图片以 `Page N image M` caption 紧跟页面文本写入占位；EPUB 用标准库 ZIP/XML 按 OPF spine 顺序读取章节，图片先按章节相对路径解析，再尝试 URI 解码/原始字面路径，保留 `#`、`?`、`%xx` 等归档文件名。文本 embedding 支持 `compat.text_as_messages`；Qwen3-VL 自托管默认启用二维 `messages` 批量会话，响应按 `index` 重排，单条输入或显式 `batch_messages=false` 保持一维会话。图片索引按 `max_image_batch_size`（Qwen3-VL 默认 5）分批，任一失败批次中的条目独立标记，不阻断其他批次。图片写入知识自有的本地或对象存储目录并标记 `extracted`，重解析/删除时清理旧文件。检索只对已开启且模型支持图片的 KB 采用图片向量，阈值 0.2，图片不参与关键词分；维度不一致、非图片、扫描页和只支持文本的模型均 best-effort 跳过/失败，不影响主解析。批量重解析、图片更新/删除、重建索引会同步刷新。图片在进入 embedding/rerank 前统一经过 imageprep：格式和大小已满足时原样透传；否则优先转 JPEG（quality 85，PNG 兜底），按 0.75 逐轮双线性缩放、最小边 64，透明背景铺白，并按模型 spec 的 `image_mime_types` 与 `max_image_bytes` 校验。VLM caption/OCR 已接入：`vlm_config` 有效（新式 `enabled + model_id`，兼容旧式 inline `model_name/base_url`）时，对每个图片块生成 caption 与 OCR，更新来源 `image_info` 并幂等写入 `image_caption`/`image_ocr` 子块；旧版 `image_processing_config.enable_multimodal` 不再作为总开关。caption prompt 使用 `description_language` 与 `custom_instructions`，OCR 使用系统固定 prompt 且不吸收自定义指令；OCR 输出先剥离代码块/HTML 包装并识别显式无文本回复，再拒绝空骨架、不可读字符、长重复片段和截断结果，状态记录为 `succeeded`/`no_text`/`failed`/`skipped`，失败码为 `OCR_INVALID_OUTPUT`/`OCR_REQUEST_FAILED`/`OCR_TRUNCATED`；无文本不落空子块，失败不影响主解析。
- 图片向量命中会向视觉聊天模型提供实际图片：仅选择 `image_vector` 结果，按 KB+URL 去重，最多 3 张，经 imageprep 限制为 5 MB、PNG/JPEG/WebP/GIF 后转 data URI；读取失败时保留 caption/OCR。上下文用 `<retrieved_images>` 标明其为不可信知识证据，用户上传图片排在检索图片之前。
- 自适应分块的 Markdown 围栏识别已按 CommonMark 收紧：反引号和波浪号围栏支持 0–3 空格缩进、至少 3 个标记、闭合长度不小于开启长度；带尾随文本的闭合符、4 空格/制表符缩进的伪闭合、不同 marker、含反引号的 backtick info string 和未闭合围栏均按同一状态机处理，代码块中的伪标题不再进入 profile、heading boundary 或 breadcrumb。
- 初始化配置的多模态与存储判定与上游一致：`GET /initialization/config/{id}` 的 `multimodal.enabled` 由有效 `vlm_config`、legacy `cos_config` 凭据/桶或非 `local` 存储 provider 决定，不再读取 `image_processing_config.enable_multimodal`；响应按实际存储输出 `storageType` 与 `cos`/`minio` 详情，共享接收方隐藏桶、区域等基础设施信息。`PUT` 不再写回旧开关，并校验/持久化 `storageProvider`、`storageBackendId`，已有文件时禁止切换存储后端；VLM/ASR 按完整配置语义重建，仅显式启用且模型可见时保留，关闭或模型不存在时清空模型 ID。
- 知识库 `PUT /knowledge-bases/{id}` 现可更新 `vlm_config`：只接受 `enabled/model_id/description_language/custom_instructions`，模型必须存在且类型为 `VLLM`，自定义图片指令上限 4000 字符。请求中的 legacy `model_name/base_url/api_key/interface_type` 只用于 GET-then-PUT 回显判断，绝不写入；启用托管模型会清空 legacy 端点，纯 legacy KB 的回显或省略 `model_id` 启用保持原配置，显式关闭且不回显时清除 legacy 字段。
- 知识库更新权限已按上游细分：共享 `editor` 可更新名称、描述和普通配置，跨租户更新按已校验的 KB ID 落库；只有 KB 所属租户或共享 `admin` 可提交顶层 `vlm_config`，共享 `editor`/`viewer` 提交时返回 403 且不产生部分更新。
- 初始化配置的敏感信息与模型校验已对齐：只有 Admin/Owner 或具备 `manage_tenant_settings` 的 API Key 能看到模型 baseUrl/存储详情，builtin 模型始终隐藏 baseUrl/API Key；PUT 校验 LLM/Embedding 模型可见，已有文件时拒绝切换 Embedding 模型；`questionGeneration` 在响应中返回并限制为 1–10 条。
- 音视频入库已与上游对齐：视频扩展名在上传入口直接返回 400“暂不支持上传视频文件”，不创建知识；worker/reparse 仍保留“暂不支持视频文件”的 failed 兜底。音频入口要求 KB `asr_config.enabled + model_id`，未配置时直接返回 400“上传音频文件需要设置ASR语音识别模型”；配置有效后上传和重解析都会调用 `/audio/transcriptions`，`language` 作为可选 form 字段传给供应商。供应商失败（包括非法 JSON）会落 `failed` 知识并记录 `audio transcription failed: ...`；空转写按上游写入 `[No speech detected in audio file]` 并完成解析。
- 知识文件上传准入已按 Go 入口补齐：只接收 `file` 单文件，校验扩展名白名单、`MAX_FILE_SIZE_MB`（默认 50）、JSON 内容、`metadata`/`process_config`/`enable_multimodel` 格式、`fileName` 相对路径与 `folder_path`；同 KB 同名称/类型/大小/hash 重复上传返回 409 `duplicate_file` 与已有知识。成功响应为单个知识对象，不再返回数组；标题保留完整文件名，`metadata`、`channel` 与目录路径落库。FAQ 知识库上传返回 400。
- `process_config` 已从“仅校验格式”补齐为实际生效的单次上传覆盖：分块配置按字段合并，VLM/ASR/问题生成配置整体替换，图片属性/向量开关与 extract 配置合并；图片/音频准入、ASR 转写、分块入库、Wiki 与图片索引均使用有效配置，覆盖持久化到 `metadata.process_overrides`。表单 `enable_multimodel` 只在 `process_config` 未显式提供时生效。
- 重解析已沿用同一覆盖语义：单条重解析可提交新的 `process_config`，先校验 VLM/ASR 准入再持久化；未提交时使用上传时保存的覆盖。单条、批量重解析及移动 `reparse` 模式均按合并后的配置重新分块并刷新索引。
- 运行时队列的 6 个 worker pool 并发改为读取 `asynq.*_concurrency` 系统设置，未配置时回退对应 `WEKNORA_*` 环境变量和默认值；`upstream_concurrency`、`parse_concurrency`、`wiki_concurrency` 与 pool 配置保持一致。
- Go wire-shape parity 已完成一轮：错误统一为 `{"success":false,"error":{code,message,details}}`，认证中间件保留纯 `{"error":...}`；会话/租户/管理员列表、审计游标、browser 空状态、用户/租户/解析器/系统信息字段及跨空间双门禁已对齐。6 个内置 Agent（含两个内部 Agent）与 29 个模型 provider 的目录、字段、顺序和本地化文案已按 Go 响应完整对齐；内置 Agent 名称/描述支持 `WEKNORA_LANGUAGE` 与 `Accept-Language` 覆盖，Agent 编辑器的 7 组提示词占位符和 5 类类型预设也已按 Go 的分组、i18n 与 omitempty 语义对齐。`/agents/{id}/suggested-questions` 已从静态字符串改为 Go 的 `{"questions":[...]}` 结构，并按 agent config、FAQ、文档生成问题和 Wiki fallback 解析候选；`tag_scopes` 会解析文档标签与 FAQ 标签，KB 限定 API key 会拒绝 `knowledge_ids`/标签并裁剪隐式 Agent KB，embed 端点复用相同 locale 优先级并转发 Agent 选中的 KB。IM 渠道的 Agent 列表与跨 Agent overview 已使用 Go 的脱敏 summary 字段，分别隐藏凭证并把内置 Agent 名称按 locale 重定位。租户落库的内置 Agent 会保留自定义 config 并在读取/复制时使用，内部 Agent 不进入列表，单资源响应不再暴露 `runnable_by_viewer` 或额外 `creator_name`。29 个关键只读接口黑盒对比中 28/29 状态码一致、25/29 字段树一致；剩余仅是 `/tenants/1` 固定 fixture 路径和示例知识库、环境向量库/存储实例的数据差异。
- 当前实现：`app/`；数据库迁移：`migrations/sqlite/`；端到端测试：`tests/test_smoke.py`；URL 防护单测：`tests/test_url_guard.py`；摘要解析单测：`tests/test_document_summary.py`；Cube/E2B 协议、模板构建与 deep check 单测：`tests/test_remote_sandbox.py`；Docker 镜像目录/pull 与会话 shell 单测：`tests/test_docker_templates.py`、`tests/test_docker_sandbox.py`。
- 最近验证：干净临时数据库下 `444 passed, 7 skipped`，`compileall -q app tests` 通过；7 个 opt-in 集成用例覆盖真实 Docker Engine（3）、MinIO（1）和 Redis limiter（3），已分别在对应真实环境运行通过；Swagger 290 个路径及方法均有实现。
- 仓库仍无 commit，全部文件为未跟踪状态；不要擅自 commit、建分支或删除既有文件。
- 默认管理账号：`admin@weknora.local / WeKnora@2026`。

## 运行与验证

```bash
cd /home/PyWeKnora
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
bash scripts/start.sh
```

```bash
cd /home/PyWeKnora
/home/ikc-log-center/.venv/bin/python -m pytest -q
/home/ikc-log-center/.venv/bin/python -m compileall -q app tests
```

默认地址：UI `/`、健康检查 `/health`、Swagger `/swagger`、OpenAPI `/swagger/openapi.json`。测试使用临时 `WEKNORA_DATA_DIR`，不要写入真实 `data/weknora.db`。

## 代码约定

- 路由放 `app/routers/`，业务逻辑尽量放 `app/services/`，数据库访问统一走 `app/db.py`。
- 成功响应优先用 `ok(...)`；业务错误用 `app/errors.py` 的 `validation/not_found/conflict/forbidden`。
- 多租户查询必须带 `tenant_id`；会话和资源访问还要校验所属用户/权限。
- 外部内容抓取使用 `app/services/url_guard.py`：每次重定向重新解析，阻断回环/私网/链路本地/IPv4 映射/6to4/NAT64 绕过，并固定到已校验 IP。
- 自托管模型、Ollama、SearXNG、MCP 等合法私网端点不要套用公网 URL 防护。
- 新增行为优先在 `tests/test_smoke.py` 增加端到端覆盖；纯安全/解析逻辑可放独立测试文件。
- 保持最小改动，不顺手重构无关代码，不修复与当前任务无关的既有问题。

## 检索意图与查询改写现状

- `app/services/query_understanding.py` 已补齐 Go `QueryIntent` 分类器：`enable_rewrite` 开启或本轮带图片时，先基于最近会话历史调用查询理解模型，返回自包含的 `rewrite_query` 与 `greeting/summarize/web_search/kb_search/image_only/doc_only/follow_up/chitchat/clarification` 意图；模型失败、输出非法或未知标签时回退原问题和空意图，空意图仍检索。
- `stream_chat_events` 在检索前应用意图门禁：`summarize`、greeting、chitchat、follow_up、image_only、doc_only、web_search 不再触发 KB 检索；`kb_search`、clarification 和空意图使用改写后的问题检索。非检索意图仍会注入对应的简短系统提示，避免继续按 RAG 资料回答。
- 意图归一化、`NeedsKBRetrieval`/`NeedsRetrieval` 语义、结构化 JSON 解析（含 markdown 包裹和截断字段恢复）均有独立测试；`rewrite_prompt_system`/`rewrite_prompt_user` 与 `query_understand_model_id` 已接入会话级 agent config。
- `web_search` 工具按会话 agent 的 `web_search_max_results` 动态设置 `limit` 上限（最高 20，默认 5），省略时使用配置上限；超过上限截断并在输出中说明，`limit < 1` 或非整数仍在调用 provider 前拒绝。

## BrowserSkill 现状

已实现：

- `app/services/browser_skill.py`：单 daemon 生命周期、`daemon.json` 启动探测、Unix socket JSON-RPC、任务启动/暂停/停止、工具调用与 `keep_open` 参数接收。
- `app/routers/chat.py`：`/api/v1/local-browser/extension` WebSocket 网关、Chrome 扩展 ID Origin/子协议/设备令牌校验、握手身份重写、双向帧转发、租约心跳与释放、`preview/focus` UI 回传通道；配对链接拒绝非本机明文 `ws://`。
- Agent 工具 `local_browser`：按方法白名单清洗参数，自动注入服务端 task/session，覆盖 `navigate` 默认 `domcontentloaded`，移除调用方伪造的 session/browser 标识。
- 轮次生命周期：agent 轮次开始时重置使用状态；成功且 `keep_open=false` 时结束轮次并回收任务，失败、取消或 `request_help` 时保留任务；删除会话时停止并清理对应任务与中断记录。
- 结果安全化：截图只向模型返回尺寸/格式等元数据，图像走视觉消息；支持视觉的模型在全部 tool 回复后收到图像，非视觉模型收到明确不可查看提示；普通结果限制为 24000 字符首尾截断。
- 错误结构：解析 daemon 的 `code/message/data`，限制超大 details 并附带恢复提示；跨副本转发超过 8 KiB 时保留 `reason/effect_state`，与 Go `RPCError.BoundDetails` 一致。
- 恢复提示已按 Go 分支细化：`tab_select` 等权限拒绝识别 borrow 文案；`tab_borrow` 区分确认 UI 不可用与确认超时/取消；补充通用 `invalid_params`、`selector_not_found`、`single_select_value_count`、fill 状态和 effect 未知/已提交提示。
- 结果解释与 Go 对齐：导航超时、evaluate 失败、request_help 未完成均判失败；显式暂停的任务必须由用户 resume，不会被下一轮自动恢复。
- `local_browser` 参数契约与 Go 对齐：工具 schema 暴露全部浏览器字段并拒绝额外字段；按方法校验允许字段、必填项、类型、枚举、范围、locator 唯一性及 emulate 互斥条件，并校验 `emulate.overrides`/`user_agent_metadata`/`request_help.targets` 的嵌套类型与 required，非法参数在派发前返回 `Invalid browser arguments`。`observe`/`snapshot` 的 `max_text_chars` 按 4 字符/Token 向上取整转换为协议字段 `max_tokens`。
- 任务状态进度已与 Go 对齐：返回当前 `action`、执行中持续增长且结束后冻结的 `action_elapsed_ms`、去除账号/query/fragment 的 `page_url`、最近错误 `last_error`，以及人类步骤进行中的 `needs_help`/`help_prompt`。
- 人类步骤超时与 Go 对齐：`request_help`/`tab_borrow` 本地 daemon 与跨副本内部 RPC 均等待 315 秒；`tab_borrow` 使用 `confirmation_timeout_ms`，`request_help` 使用 `timeout_ms` 并剔除调用方 predicate。
- 中断与取消语义与 Go 对齐：pause、`session.user_interrupt` 和 stop 会记录活动 daemon RPC 并发送对应 `cancel`；stop 先阻止新命令、取消活动命令，待同会话调用退出后再执行 `session.stop`，避免遗留后台动作。
- `tab_close` 已按 Go 语义保护 Agent Window：关闭最后一个 agent 标签前先创建 `about:blank` 占位标签；关闭失败或被中断时仅在目标标签仍存在时清理占位，避免窗口被意外销毁。
- 中断与超时恢复：`session.user_interrupt` 在网关消费并暂停任务，窗口关闭会丢弃旧任务以便下次显式 resume 新建；本地/跨副本 RPC 超时或传输失败会向 daemon 发送对应 `cancel`，避免遗留后台命令。
- 多副本路由：SQLite lease 记录 node owner，内部 RPC 使用 HMAC-SHA256、时间窗和 nonce 防重放；工具、状态、控制、预览/聚焦和轮次结束可转发到连接所有者。
- 连接与生命周期：`BROWSERSKILL_MAX_CONNECTIONS`（默认 32）在扩展握手前预留容量，daemon/lease/accept/handshake 失败会释放；应用关闭时只清理本节点的 owner lease，不触碰其他节点。stop 中的任务会忽略迟到 `session.window_closed`。
- 持久化仍沿用 `browser_devices`、`browser_pairings`、`browser_task_interruptions`。

仍缺：

- 真实原生扩展 + 原生 daemon + Chromium 的联合测试仍需 `BROWSERSKILL_TEST_BINARY`/扩展/Playwright 环境；当前已覆盖网关双向帧、UI 隔离、事件暂停、断线释放和超时 cancel 的协议级测试，仍需持续核对新增 daemon 错误码与恢复提示。

## Docker/Cube/E2B 运行时现状

已实现：

- `app/services/remote_sandbox.py`：标准库 HTTP 客户端，支持 Cube（JSON Connect）与 E2B（protobuf Connect）控制面 `health/create/exec/delete`。
- envd 数据面鉴权、`proxy_url` 网关、Host 保留、网络策略映射，以及稳定的 `authentication/not_found/timeout/unavailable/capacity/invalid_request` 错误分类与中文 reason。
- 管理端 sandbox deep check：创建临时沙箱、执行 `echo weknora-ok`、探测 baidu/1.1.1.1、销毁沙箱；策略拒绝出网时跳过 egress 检查而不是判失败。
- 管理端模板目录：`POST /sandbox-configs/templates/query` 支持 `config_id` + 脱敏 `config` 合并已存凭据，Cube/E2B 读取真实 `GET /templates`；`ensure_standard` / `ensure_desktop` 在缺失时构建，`replace_standard` / `replace_desktop` 原地重建或新建替换。Cube 使用 envd 模板规格与 DNS，E2B 使用 `v3/templates` + `v2/.../builds/...` 两步构建；替换就绪后回写配置 `template_id` 并清理旧模板。
- Docker 模板目录读取真实 daemon 镜像（`docker image ls`），过滤 skill/fork snapshots，配置镜像缺失时显示 `missing`；`ensure_standard` / `replace_standard` 在后台执行 `docker pull`，重复刷新复用进行中的任务。
- E2B 模板列表会对第一方标准/桌面模板的 stale pending 状态做校准：有 buildID 时查询 build status，buildID 为空且已有成功构建时标记 `untagged`；其他模板不做额外探测，避免 N+1。
- 会话级绑定：`migrations/sqlite/000037_session_sandboxes` 与 `app/services/session_sandbox.py` 持久化会话到 Docker/Cube/E2B 沙箱的映射；`ensure/exec_command/delete` 支持复用、环境变量、工作目录和最长 600 秒超时。
- Docker 会话 shell：`app/services/docker_sandbox.py` 用 `docker run -d` 创建会话级常驻容器，`docker exec` 执行命令；带 managed/idle TTL label、CPU/内存/PID 上限、capability drop、`bridge/none` 网络限制和临时 0600 env-file。执行前会恢复 `exited/created` 容器并 `unpause` 已暂停容器，删除会话时 `docker rm -f`；容器已被删除时 `exists()` 返回 false。
- Docker 空闲回收：create/exec 后按 endpoint 限频触发后台 sweep，读取容器内 activity marker mtime（并拒绝未来时间），只删除超过自身 TTL 的 managed 容器；session 绑定发现 Docker 容器已被回收时会自动重建，而不是永久卡在失效 sandbox ID。会话单删、批量删除和 `delete_all` 都会清理绑定沙箱。
- Agent `shell_exec` 仅在 `allowed_tools` 包含该工具且会话能解析 sandbox config 时注册，命令通过会话绑定的 Docker/Cube/E2B 沙箱执行。
- Agent `read_file` 已补齐 Go 的工作区文件语义：只要有会话 sandbox config 就注册（不要求启用 skills 或显式列入 `allowed_tools`），相对路径锚定 `/workspace`、绝对路径可读取沙箱内 `/tmp` 等位置，绝不直接打开宿主文件；读取前 stat 并拒绝目录/非普通文件，超过 8 MiB 不下载，文本默认 2000 行、单页最多 64 KiB 且受 24000 rune 输出预算约束，分页返回 `returned_bytes/truncated/total_lines/start_line/end_line/next_offset`。二进制内容只返回元数据，不内联或 base64 暴露；`skill://` 资源复用同一分页与二进制抑制路径。
- Agent `list_sandbox_files` 已补齐无 shell 时的目录发现能力：有 sandbox config 且未注册 `shell_exec` 时自动提供，递归列出沙箱内普通文件（跳过目录、符号链接和其他特殊文件），默认扫描 `/workspace/output`，相对路径锚定 `/workspace`，结果按路径排序并默认限制 200 条、最高 500 条；不存在的根目录返回空列表，非目录/列目录失败返回明确错误，不触碰宿主文件。
- Agent `write_sandbox_file` / `edit_sandbox_file` 已补齐 Go 的沙箱写入语义：仅在会话 sandbox config 存在时注册，允许 `/workspace` 下（相对路径锚定 `/workspace`）且拒绝 `/workspace/input` 与目录根；写文件支持 overwrite/append、单文件 8 MiB 上限、拒绝二进制与目录，append 要求目标是已存在的普通文件并校验累计大小；编辑要求文件存在、普通、非二进制且不超过 256 KiB，所有 `old_string` 都针对原始内容匹配，默认要求唯一，`replace_all` 可全量替换，重叠编辑整批拒绝，任一校验失败不落盘，结果只返回路径/替换数/大小而不回显内容。沙箱上传通过本地 base64 分块执行，先写同目录临时文件再原子 `mv`，避免正文经过 shell 转义并防止半写文件。
- 终端协议：`POST /sessions/{id}/sandbox/terminal-ticket` 签发 120 秒票据并绑定签发者的 `auth_tokens` access token；`WS /sessions/{id}/sandbox/terminal` 校验票据与 URL session 绑定、token 未撤销、用户/会话归属和 `provision`，`ready` 返回真实 backend、`pty_id` 与 `reattachable`。二进制帧原样写入 PTY，文本帧处理 `resize`/`ping`，PTY 输出也刷新 15 分钟空闲计时；每 60 秒复查 access token/用户/会话，每会话单进程最多 5 个 PTY。
- 真实 PTY：Docker 使用 Engine API `ExecCreate(Tty=true)` + 升级连接读取原始字节流，`ExecResize` 处理窗口尺寸，关闭时按终端标记清理容器内 shell 进程树；Cube/E2B 使用 envd `Process.Start/Connect` 流式事件，支持 `SendInput`、`Process.Update`、按 PID 重连和原始输出。Docker 明确不可重连（每次都新建 shell），Cube/E2B 返回可重连 PID。
- 桌面协议：`POST /sandbox/desktop-ticket` 与 `WS /sandbox/desktop` 已接入；后端执行镜像内 `start-desktop.sh`，从 `/run/desktop/secret` 读取 Basic 凭据，并先校验数据面返回的 `RFB 003.008` 版本帧，再中继 websockify(6080) 的 RFB 二进制帧。凭据和 traffic token 只存在于后端到 sandbox 的一跳。
- desktop 协调：迁移 `000038_desktop_leases` 提供 45 秒 SQLite lease 与 15 秒续租，跨副本保证单会话一个桌面；15 分钟无活动关闭 `IDLE_DISCONNECTED`，每 60 秒复查 access token/用户/会话并关闭 `AUTH_REVOKED`。
- desktop 重建提示：迁移 `000040_desktop_last_sandboxes` 记录每个会话最近桌面连接的 sandbox ID（7 天 TTL）；重连到不同 sandbox 时关闭 `SANDBOX_REBUILT`。
- 票据安全：迁移 `000039_sandbox_tickets` 持久化 ticket `jti`，terminal/desktop WebSocket 在鉴权后原子消费；ticket 绑定签发 access token 的 `auth_tokens.id`，撤销后握手与周期复查均返回 `AUTH_REVOKED`；同一 ticket 的第二次连接返回 `AUTH_REVOKED`。
- `shell_exec` 可见输出按 Go 契约限制为 64 KiB，并按 1:3 保留头尾；`ensure()` 落库失败会立即删除刚创建的远端沙箱。

仍缺：

- Docker Engine 的 create/exec/环境变量/TTY Exec/stop 后自动启动/pause 后自动 unpause/删除/Managed idle sweep 已用本地镜像完成真实集成验证；Cube/E2B 仍只有协议级与 fake 流测试，真实云端 PTY/desktop 尚未联调。access token/用户/会话周期复查与每会话 5 个 PTY 上限已实现；该上限与上游一致，为单进程计数器，跨副本不需要终端级协调。
- desktop 跨副本租约仍依赖共享 SQLite。
- Docker desktop 明确返回不支持；对象存储上传/读回/服务端 CopyObject/幂等删除已用真实 MinIO 验证，presigned GET/HEAD 仍为签名单测与 mock 闭环；S3/COS/TOS/OSS/KS3/OBS 尚未用真实账号端到端联调。
- 尚未用真实 Cube/E2B 云端账号做原生联调；当前由协议级测试覆盖。
- Docker 会话容器、Engine PTY、模板目录与后台 pull 状态机已用真实 daemon 验证；成功拉取远程镜像仍需可访问的 registry，当前只用无效引用验证真实失败路径。
- 外部向量库目前只有注册/CRUD 元数据与连通性测试路径，实际检索运行时尚未接入。
- 图片多模态向量已覆盖 Markdown/HTML 引用、docx/pptx/PDF/EPUB 内嵌图片与直接图片上传，embedding/rerank 前已接入上游 imageprep parity；VLM caption/OCR 与音频 ASR 已接入，视频按上游明确拒绝。
- 网络搜索的 SSRF、白名单、固定 IP 和代理路径尚未用真实私有 SearXNG/真实 HTTP 代理做端到端联调；当前由 mock 与单测覆盖。
- DingTalk 在线文档与上传 Office/PDF 目前由 fake HTTP/API 测试覆盖，尚未用真实企业租户凭据联调；上传文件按纯文本提取，尚不保留内嵌图片或原始二进制。
- Seafile 目前由 fake api2/fileserver 与单元测试覆盖，尚未用真实 Seafile 10.0.1 租户联调；目录失败仍会中止本轮且不保存同步中的增量检查点，图片/音频虽通过扩展名过滤但会在文本提取阶段失败。
- WeCom 长连接尚未用真实企业微信 `bot_id/bot_secret` 联调；图片/文件消息、后台知识库入库与去重已由 fake HTTP/API 与单元测试覆盖，但尚未验证真实租户的临时下载 URL、真实 Office/PDF 样本和企业微信视觉模型链路。视频仍明确回复不支持。
- WeCom 长连接租约依赖共享 SQLite 数据库；若多个副本各自使用独立数据库，仍会重复订阅，需要部署时共享同一数据库或后续改为 Redis 租约。

## 待办优先级

1. BrowserSkill 收尾：原生扩展/daemon 联合测试，并持续核对新增错误路径。
2. Docker/Cube/E2B 收尾：Docker 会话容器/Engine PTY、模板目录与真实 pull 失败状态已完成联调；仍需真实 Cube/E2B 云端 PTY/desktop 联调，以及用可访问 registry 验证成功 pull。terminal 周期鉴权与单进程并发上限已完成（与上游一致）；desktop 的跨副本/空闲/重建提示已完成，租约仍依赖共享 SQLite。
3. 剩余占位/投影接口审计：运行时队列已支持 SQLite 任务表查询/取消/删除/归档清理，并区分 pending 与 retry；FAQ、复制、移动、重解析均有后台 worker、SQLite 跨进程租约、启动恢复、`run_now` 和类型特定重试预算（FAQ 5/3 次，其余 3 次）；跨进程模型并发协调已接入 Redis limiter，但仍缺 Redis/asynq 多队列、scheduled 调度、权重调度与更细的中断恢复；外部向量库运行时仍未接入。
4. 其他已知差异：存储配额预占/调整/释放与超限 chunk 清理已接入；`resource://` 目录的文本/图片 claim、克隆再 claim、删除 release 与最后引用回收已接入。Python 直传本地/对象存储仍沿用既有 provider 路径，不额外注册 catalog handle；`dataset` QA 工具在当前 Python 工具面中尚未提供。
5. 云存储收尾：对象存储上传/读回/幂等删除、源文件及 chunk 内嵌图片服务端复制已完成真实 MinIO 验证；仍需用真实 S3/COS/TOS/OSS/KS3/OBS 账号联调 CopyObject 兼容性。
6. 完整 parity 审计：逐模块核对错误码、权限、分页、SSE 事件、取消与异常恢复。2026-10-18 已完成最近 86 个未记录上游提交的逐项处置，其中 10 项已实现、其余按已覆盖或缺少对应子系统归档；仍需继续核对新增提交和已确认的更大范围缺口。
7. 外部连接器与长连接：DingTalk 在线文档与上传 Office/PDF 下载/文本解析已落地，仍需真实凭据联调和上传文件内嵌图片保留；Seafile 资源浏览、增量/全量同步、库绑定、失败重试与删除对账已落地，仍需真实租户联调并补齐目录失败检查点和媒体入库语义；WeCom 长连接文本/语音、图片/文件 AES 下载、附件入模、后台知识库入库与去重、跨进程单订阅租约已落地，但仍需真实企业微信凭据和真实媒体样本联调，视频仍不支持。

## 完成判定

只有同时满足以下条件才可认为复刻完成：Swagger 路由全部覆盖；前端关键调用路径无 404/占位响应；权限与租户隔离有测试；长任务可取消并可恢复；BrowserSkill/Cube 等运行时具备真实执行路径；全量测试与编译通过；`process.md` 和本文件的待办已清零。
