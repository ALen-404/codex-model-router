# Codex 本地模型路由器（opencode + Cline / DeepSeek 系列）

把两个上游的 DeepSeek 模型整合进 Codex 的「选择模型」统一列表，每个模型带
**实测的思考档位**、**实测的上下文上限**，并打开上下文用量显示。

| 上游 | 端点 | 协议 | 模型 |
|---|---|---|---|
| opencode（Go 计划） | `https://opencode.ai/zen/go/v1` | responses 直通 | **`deepseek-v4.1-flash-opencode`** |
| Cline（订阅额度） | `https://api.cline.bot/api/v1` | **chat + 双向翻译**（Cline 不提供 /responses） | **`deepseek-v4.1-flash-cline`** |

- 只依赖 Python 标准库，无第三方包
- 密钥只放用户级环境变量：`OPENCODE_API_KEY`、`CLINE_API_KEY`

---

## 1. 为什么要中间层

Codex 的一个 provider 只能绑定一种 wire 协议（`wire_api = "responses" | "chat"`），
而"一个列表里混多家上游 + 多种协议"需要运行期按模型分发。所以：

```
┌──────────────────────────────────────────────────────────────────────┐
│ Codex 桌面端 / CLI                                                    │
│   config.toml:                                                       │
│     model_provider = "local-router"                                  │
│     model          = "deepseek-v4.1-flash-opencode"                  │
│     model_catalog_json = ~/.codex/model-router/model-catalog.json    │
│     [model_providers.local-router]                                   │
│       base_url = "http://127.0.0.1:8791/v1"                          │
│       wire_api = "responses"          ← Codex 只会说 Responses        │
│     [desktop] show-context-window-usage = true                       │
└───────────────────────────────┬──────────────────────────────────────┘
                                │ Responses API (POST /v1/responses, SSE)
                                ▼
┌──────────────────────────────────────────────────────────────────────┐
│ router.py   127.0.0.1:8791                                           │
│   1. 读请求体里的 model，查 router-routes.json                        │
│   2. 密钥按 key_env 从「用户级环境变量」读（永不落盘）                  │
│   3. 先做请求归一化 normalize_responses_input：                      │
│        Codex 自己会写出上游不接受的工具条目（定时任务心跳缺 call_id）， │
│        这里把它降级成 user 消息 —— 两条 wire 都过，正常请求零改动       │
│   4. 按 wire 分流：                                                   │
│        wire="responses" → 直通上游 /responses（只改模型名 + 夹档位）    │
│        wire="chat"      → translate.py 双向翻译 ↕ /chat/completions   │
│   5. 403/408/429/5xx/TLS 断连 → 指数退避重试                          │
│   6. 日志 ~/.codex/model-router/router.log                            │
└───────────────────────────────┬──────────────────────────────────────┘
                                │ 浏览器 UA + x-opencode-session
                                ▼
                    opencode.ai/zen/go/v1
```

翻译层的三个实测坑（详见 §6）都实现在 `translate.py` 里，且有单元测试与真实端到端验证。
另有一个**不属于翻译层、两条 wire 都要处理**的坑：Codex 定时任务心跳写出的
`function_call_output` 缺 `call_id`，上游反序列化直接 400 —— 由
`normalize_responses_input()` 统一修掉（详见 §6 坑 4）。

---

## 2. 交付物

| 文件 | 作用 |
|---|---|
| `router.py` | 本地路由器（Responses API 服务端 + 上游分发） |
| `translate.py` | Responses ⇄ Chat Completions 双向翻译（请求 / 非流式 / SSE 状态机） |
| `probe-upstreams.py` | 上游探测脚本：wire 支持、思考档位白名单、models.dev 元数据对照 |
| `model-metadata.json` | **唯一人工维护源**：模型事实 + 每个数字的来源说明 |
| `build-router-catalog.py` | 读元数据表 → 生成 `router-routes.json` + `model-catalog.json` |
| `install-model-router.py` | 幂等安装（备份 + 条件重启 + `--rollback` / `--status` / `--dry-run`） |
| `check-model-router.ps1` | 健康检查（配置/目录/路由/密钥/进程/端到端） |
| `tests/test_translate.py` | 翻译层单元测试（27 项） |
| `model-catalog.json` | 生成的合并目录（Codex 读它渲染「选择模型」） |
| `router-routes.json` | 生成的路由表 |
| `docs/evidence/upstream-probe-results.json` | 探测原始证据（每个模型每个档位的 HTTP 状态） |
| `docs/evidence/effort-measure.json` | 各档位实际推理 token 数（证明档位真的分级） |
| `docs/evidence/context-limit-probe.json` | 上下文/输出上限的超限探测原始报错 |
| `docs/evidence/cline-probe.json` | Cline 的探测原始结果 |
| `docs/evidence/cline-metadata-notes.json` | Cline 端点事实与实测结论 |
| `archived-model-entries.json` | 按需归档的模型条目（可粘回元数据表恢复） |

安装后的运行时布局：

```
~/.codex/model-router/
  router.py  translate.py  router-routes.json  model-catalog.json
  router.log  router.out.log  router.pid  install-state.json
```

---

## 3. 每个数字的来源

**原则：不猜。** 能用上游自报的用上游，其次实测，models.dev 仅作对照。

| 数字 | 值 | 来源 |
|---|---|---|
| 上下文上限 | **1 048 576** | 实测：故意超限请求，上游回 `This model's maximum context length is 1048576 tokens`。5 个模型逐一测过，全部一致 |
| 最大输出 | **393 216** | 实测：`max_tokens=5000000`，上游回 `the valid range of max_tokens is [1, 393216]` |
| 思考档位白名单 | `none, minimal, low, medium, high, xhigh, max` | 实测：7 个候选值全部 200；非法值（`bogus` / `123` / 空串）全部 400 → 说明确有白名单，且这 7 个都在里面 |
| 档位是否真生效 | 是 | 实测：非法值被拒 + `none` 档无 `reasoning_content`（思考关闭）而其余档位有；各档位推理 token 数不同（如 deepseek-v4-pro：minimal 71 / low 80 / medium 73 / high 66 / xhigh 78 / **max 119**）。见 `docs/evidence/effort-measure.json` |
| 图片输入 | 5 个模型全部支持 | 实测：8×8 纯绿 PNG，5 个模型都正确回答绿色。**models.dev 对其中 3 个标 `attachment=false`，与实际不符，以实测为准** |
| `wire` | 5 个主条目用 `responses` | 实测：5 个模型的 `/responses` 与 `/chat/completions` 都能通；直通无翻译风险，故默认直通，另留 1 个 `chat` 条目验证翻译层 |
| models.dev 标称值 | context 1000000 / output 384000 | 与上游强制值**不一致**，采用上游报出的 1048576 / 393216 |
| `x-opencode-session` | 必需 | 实测：不带该头，`/responses` 与 `/chat/completions` 均返回 `400 MissingSessionID` |
| 浏览器 UA | 必需 | 上游挂 Cloudflare，urllib 默认 UA 会 403 |
| `unified_exec` | 合法 `shell_type` | codex.exe 内含该字面量（122 处） |
| `show-context-window-usage` | 合法设置键 | app.asar 内设置定义为 `key: 'show-context-window-usage'`（kebab） |
| `model_catalog_json` | 合法配置键 | codex.exe（21 处）与 app.asar（4 处）均含 |

### Cline（`deepseek/deepseek-v4.1-flash`）的数字来源

| 数字 | 值 | 来源 |
|---|---|---|
| `wire` | **`chat`** | 实测：`POST /api/v1/responses` 返回 **404 Not Found**（无鉴权时的 401 只是鉴权门先拦住），只有 `/chat/completions` 可用 → 必须走翻译层 |
| 上下文上限 | **1 048 576** | 实测：~1.1M tokens 输入，上游报 `This model's maximum context length is 1048576 tokens` |
| 最大输出 | 384 000 | **唯一非实测项**：models.dev `cline-pass` 标称值。实测网关**不校验**（`max_tokens=5000000` 仍返回 200），拿不到强制上限，只能取标称 |
| 思考档位 | `none…max` 共 7 档 | 实测：7 档全部 200；`none` 档 `reasoning_tokens=0` 且消息无 `reasoning` 字段（思考关闭）；`max` 档推理量最大（94 tokens）。models.dev 只声明 5 档（无 `minimal`/`max`），以实测为准 |
| 图片输入 | 支持 | 实测：8×8 纯品红 PNG 正确答出 `Magenta` |
| 工具调用 | 支持 | 实测：流式 `delta.tool_calls` 与非流式 `message.tool_calls` 都是标准 OpenAI 形状，`finish_reason=tool_calls` |
| 响应结构差异 | 见 §6 坑 6 | 实测：推理字段叫 `reasoning`；非流式响应外包一层 `{"data":…,"success":true}` |


> 上表中 `deepseek-v4-pro` / `deepseek-flash` 等模型名是**当时探测时**的记录，这些模型现已按用户要求归档（见 `archived-model-entries.json`），保留原文以便追溯数字来源。

`deepseek-flash` **不在 models.dev 的 opencode-go 目录里**（该目录 36 个模型，线上 `/v1/models` 有 37 个）。它的所有数字都来自上面的超限/档位实测，没有任何一项是推测。

---

## 4. 安装与验证

```bash
cd outputs
python build-router-catalog.py          # 生成目录与路由表（改元数据后重跑）
python install-model-router.py --dry-run # 先看会改什么
python install-model-router.py          # 安装（备份 config.toml、按需启动路由器）
powershell -ExecutionPolicy Bypass -File check-model-router.ps1
```

安装脚本是幂等的：

- 只有 **路由代码 / 路由表** 变化才重启路由器；**仅目录变化不重启**（Codex 自己热读目录）
  指纹记录在 `install-state.json`
- `config.toml` 每次改动前备份为 `config.toml.bak-router-<时间戳>`
- 只做外科式改动，你原有的 `[model_providers.opencode]`、`[model_providers.fast]`
  等段落原样保留

**路由器必须能独立存活**，否则 Codex 会收到 `502 Bad Gateway`（不是上游的错，是路由器死了）：

- 启动用 `CREATE_BREAKAWAY_FROM_JOB | DETACHED_PROCESS`。只用 `DETACHED_PROCESS`
  时，如果启动它的父进程处在 Job Object 里（脚本/CI 会话），路由器会随父进程一起被杀
  —— 这已经真实发生过一次：进程消失、日志无 traceback、Codex 侧只看到 502。
- 同时往当前用户的 **Startup 文件夹**写自启项
  `%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\CodexModelRouter.cmd`，
  解决重启后的问题。**没用 `schtasks`**，因为建计划任务需要管理员权限（实测被拒）。
- `--rollback` 会一并移除自启项。

---

## 5. 怎么加新模型

1. 先探测，别猜：

   ```bash
   python probe-upstreams.py --only <上游模型 id>
   ```

   再对上下文/输出上限做一次超限探测（照 `docs/evidence/context-limit-probe.json` 的做法），
   并测一次图片（若该模型号称支持视觉）。

2. 在 `model-metadata.json` 的 `models` 数组里加一条，字段照现有条目补全，
   **在 `sources` 里写清每个数字怎么来的**。

3. `python build-router-catalog.py && python install-model-router.py`

4. 重启 Codex 桌面端，在新列表中应能看到该模型。

加**新上游**：在 `providers` 里加一项（`upstream_base` / `key_env` / 可选
`session_header`），然后用 `setx <KEY_ENV> "<key>"` 把密钥写进用户级环境变量。
若该上游只支持 chat，把模型的 `wire` 设成 `"chat"` 即可复用翻译层。

### 例：Cline 的 `deepseek/deepseek-v4.1-flash`（已完成，可作模板）

Cline 已接入，slug 是 **`deepseek-v4.1-flash-cline`**（不能直接用
`deepseek-v4.1-flash`，因为 slug 同时是路由表的键，会和 opencode 那条撞车）。
它是**唯一走 `wire="chat"` 的正式条目**，所以也是翻译层的实战路径。

如果以后要换 key 或加 Cline 的别的模型，流程是：

```bash
# 1. 从 https://app.cline.bot/dashboard 拿 API Key，写进用户级环境变量
setx CLINE_API_KEY "<你的 key>"      # 新开一个终端才生效

# 2. 实测（模型列表、协议、档位、上下文、图片、工具调用）
python probe-upstreams.py --base-url https://api.cline.bot/api/v1 \
    --key-env CLINE_API_KEY --models-dev-provider cline-pass \
    --session-header "" --only deepseek/deepseek-v4.1-flash --out docs/evidence/cline-probe.json
#   注意：探针的 max_tokens 太小会被推理吃光，Cline 会回 500 "empty response content"，
#   所以要给足输出预算；超限探测照 docs/evidence/context-limit-probe.json 的做法读上游自报上限

# 3. 在 model-metadata.json 的 models 数组里加一条（provider: "cline"），然后
python build-router-catalog.py && python install-model-router.py
```

已确认的端点事实（也记在 `docs/evidence/cline-metadata-notes.json`）：`/chat/completions`、
`/responses`、`/messages` 三条路径都存在，但**只有 `/chat/completions` 可用**
（另两条 404）；鉴权是 `Authorization: Bearer <CLINE_API_KEY>`；公开模型列表 447 个，
deepseek 系 21 个。

---

## 6. 已知限制

**翻译层（wire="chat"）的三个坑与对策**（都有单测 + 真实端到端验证）：

1. **带 `tool_calls` 的 assistant 消息必须回传 `reasoning_content`**，否则上游 400。
   对策：`ReasoningCache` 按 `call_id` 缓存上一轮推理文本，下一轮补回；未命中用
   占位文本 `(reasoning not available for this call)` 兜底。
   验证：日志出现 `split 1 image(s) out of tool result` 后的**第二次**请求成功，
   证明带 tool_calls 的历史回传没被拒。
2. **chat 的 `role:"tool"` 消息不能带图片**（400 Invalid input）。
   对策：图片从 tool 结果里拆出、挂起到**整组并行 tool 消息结束之后**，作为随后的
   user 消息发送。
3. **`tool_calls` 与 tool 结果必须配对**。对策：缺失的补
   `(no tool result was recorded for this call)`；找不到对应调用的孤儿 tool 结果
   降级成 user 消息。连续的多个 `function_call` 会**合并进同一条 assistant 消息**
   （chat 的并行调用形状），而不是拆成多条。

4. **Codex 定时任务心跳写出的 `function_call_output` 没有 `call_id`**（两条 wire 都受影响）。
   实测样本（`~/.codex/sessions/2026/09/05/rollout-...01a071c4...jsonl`）：

   ```json
   {"type":"function_call_output","id":"fco_01a09138-7577-7813-be96-a26aa55e898a",
    "name":"automation_update","namespace":"codex_app",
    "output":"<heartbeat><automation_id>keeper</automation_id>..."}
   ```

   注意它有 `id` / `name` / `namespace`，但**没有 `call_id`**。配置了每小时的定时任务后，
   这种记录会持续追加进会话，回放历史时上游报
   `Failed to deserialize the JSON body into the target type: input: missing field 'call_id'`。

   四种修法实测结果（用真实上游逐一打过）：

   | 修法 | 结果 |
   |---|---|
   | 原样发送（复现） | 400 `missing field 'call_id'` |
   | **降级成 user 消息** | **200 ✓（采用）** |
   | 只补 `call_id`、不补配对调用 | 400 `No tool call found for tool output with call_id ...` |
   | 补 `call_id` + 伪造配对 `function_call` | 400 `The reasoning_text in the thinking mode must be passed back to the API` |

   所以 `normalize_responses_input()` 只做这一件事：**无主/缺 `call_id` 的工具输出 →
   降级为携带原文的 user 消息**；声明型条目缺 `call_id` 则用其 `id` 顶替。
   对形状正常的请求是零改动，日志会留下
   `WARN 请求归一化 ["normalized 1 tool item(s) missing call_id -> user message"]`。

   全库扫描结果：31 438 条 `response_item` 里只有上述 1 个文件的 5 条记录有此问题，
   其余全部合法。

5. **目录文件被手改坏会让整个 Codex `config_load` 失败**（不是"目录不可用"，是应用起不来）。

   实测事故：运行时目录 `~/.codex/model-router/model-catalog.json` 被人工/代理编辑过，
   出现两类问题，新版 Codex（26.908 / CLI 0.154.0-alpha.6.2）直接拒绝加载：

   ```
   failed to load configuration: failed to parse model_catalog_json path
   `...model-catalog.json` as JSON: unknown variant `hidden`,
   expected one of `list`, `hide`, `none` at line 76 column 5
   ```
   ```
   failed to parse model_catalog_json path `...` as JSON: duplicate field `visibility`
   ```

   两个关键事实：

   - `visibility` 的合法取值只有 **`list` / `hide` / `none`**。写 `hidden` 会让
     `config_load` 失败，进而 **Windows 沙箱一次性设置流程也跟着失败**
     （界面表现是「完成 Windows 设置 · Windows 安装未完成 · config_load」）。
   - **Rust 的 serde 拒绝重复 JSON 键**，而 Python 的 `json` 会静默取最后一个。
     所以手改文件时很容易做出"Python 能读、Codex 读不了"的目录。

   对策（三层防护，都已落地）：

   - `build-router-catalog.py`：生成前校验元数据里的 `visibility` / `shell_type` 枚举，
     生成后用 `object_pairs_hook` 严格重解析（重复键直接报错），校验失败退出码非 0。
   - `install-model-router.py`：安装前对**待装目录**做同样的严格校验，不通过就拒绝安装。
   - `check-model-router.ps1`：校验运行时目录的枚举值，非法直接判 FAIL 并指明
     "会让 Codex config_load 失败"。

   如果你要手工调整哪个模型可见，改 `model-metadata.json` 里的 `visibility` 再重跑
   build + install，**不要直接编辑运行时那份目录**。

6. **不同上游的 chat 方言不一样，翻译层必须同时认**（接入 Cline 时实测发现）。

   | | opencode | Cline |
   |---|---|---|
   | 推理字段 | `delta.reasoning_content` | **`delta.reasoning`**（另有 `reasoning_details` 数组） |
   | 非流式响应 | 标准 OpenAI 结构 | **包一层 `{"data": {…}, "success": true}`**；流式**不包** |
   | `/responses` | 支持 | 404，只能 chat |
   | 工具调用 | 标准 `tool_calls` | 标准 `tool_calls`（一致） |

   对策：`_delta_reasoning()` / `_message_reasoning()` 同时接受 `reasoning_content` 与
   `reasoning`（以及 `reasoning_details` 数组拼接）；`unwrap_chat_payload()` 负责解
   `data` 包装。**注意流式与非流式的包装不一致**，所以这两条路径要分别处理。
   已有单元测试覆盖（`TestClineWireCompat`），并用真实 `codex exec` 端到端验证过。

   再一个上游特性：Cline 网关会在 **14 家推理提供商之间做回退路由**
   （实测一次请求落在 `alibaba`，`fallbacksAvailable` 里还有 baseten / fireworks /
   novita / togetherai / deepinfra 等）。这意味着同一模型的输出风格、延迟可能在不同
   请求间漂移 —— 这不是本路由器能控制的，只能知道。

其他限制：

- **只接了 DeepSeek 系**。opencode 端点还有 31 个非 DeepSeek 模型（GLM / Kimi / Qwen /
  MiniMax / Grok 等），按同样流程加元数据即可接入，但本次未纳入。
- **目录里 5 个模型全部按 `wire="responses"` 直通**，所以它们走不到翻译层；
  翻译层由 **`deepseek-v4.1-flash-cline`** 承载 —— Cline 只提供 chat/completions，
  所以它是正式条目里唯一走 `wire="chat"` 的，翻译层是它的必经路径而非备用通道。
- **`deepseek-v4-flash` 的 usage 不返回 `reasoning_tokens`**（恒为 0），但
  `reasoning_content` 有输出。想看思考内容不要依赖 token 计数。
- **上游不校验档位语义**，只校验白名单：7 个档位都接受，但档位间的强度差异在上游侧
  实现，本路由器只做白名单夹取（非法值→最近合法档位；完全未知→不传）。
- **路由器无鉴权**，仅监听 `127.0.0.1`。不要改成对外监听。
- **`ReasoningCache` 是进程内内存**，重启路由器后旧会话的 `reasoning_content` 会退化为
  占位文本（只影响 wire=chat 通道的多轮工具续聊）。
- **`model_reasoning_effort` 保持你原来的 `"max"` 未动**，它会作为全局默认作用到所有
  模型（5 个模型都支持 max，实测通过）。

---

## 7. 回滚

```bash
python install-model-router.py --rollback
```

它会：把 `config.toml` 还原成安装前备份，并停掉路由器。之后 Codex 回到安装前的
provider 配置（`model_provider` 指回 `opencode`）。

只想临时停用而不还原配置：

```bash
python install-model-router.py --status          # 看状态
python -c "import sys;sys.path.insert(0,'.');import install_model_router" # 或直接:
taskkill /PID $(cat ~/.codex/model-router/router.pid) /F
```

手工回滚：把 `~/.codex/config.toml.bak-router-*` 覆盖回 `config.toml`，
删除 `~/.codex/model-router/`，再用 `setx OPENCODE_API_KEY ""` 清掉密钥（可选）。

---

## 8. 验证记录

| # | 验证项 | 结果 |
|---|---|---|
| 1 | 翻译层单元测试 | `Ran 33 tests — OK`（含并行调用合并、图片拆分、配对、档位夹取、SSE 事件序列、请求归一化） |
| 2 | `codex exec -m` 工具调用 | 两条通道都通过：`-m deepseek-v4.1-flash-opencode`（responses 直通，回读 `OC-OK`）与 `-m deepseek-v4.1-flash-cline`（chat 翻译，回读 `CL-OK`）；升级到 CLI 0.154.0-alpha.6.2 后复测仍通过 |
| 3 | `view_image` 看纯色图 | 两条通道都通过，且从 rollout 坐实真调了工具：`function_call view_image` + 工具返回含 `input_image`；chat 通道另在路由器日志中留下 `split 1 image(s) out of tool result` |
| 4 | 约 10 万 token 请求 | `input_tokens = 100,023`，`status = completed`，收到 `response.completed` |
| 5 | `codex debug models` | 6 个条目全部出现，`context_window = 1048576`、7 个档位、visibility 正确 |
| 6 | wire=responses 回归 | `deepseek-v4.1-flash-opencode` 直通正常（工具调用 + 长上下文均通过）；原有 `opencode` / `fast` provider 段落完好 |
| 7 | `check-model-router.ps1` | **通过 34，失败 0，警告 0** |
| 8 | 定时任务心跳 400 的修复（A/B） | 旧代码：同一 payload → `HTTP 400 missing field 'call_id'`；新代码：`HTTP 200 status=completed`，日志 `normalized 1 tool item(s) missing call_id -> user message` |
| 9 | 路由器跨进程存活 | `CREATE_BREAKAWAY_FROM_JOB` 启动后，在**另一个独立进程**里复查：进程存活、`/healthz` 200 |
| 10 | 坏目录致 `config_load` 失败 | 修复前：新版 CLI `exit=1` + `unknown variant hidden` / `duplicate field visibility`；修复后：`exit=0`、stderr 为空、6 个模型全部解析 |
| 11 | 安装脚本不覆盖用户选择 | 二次安装输出「配置改动（无，已是目标状态）」，`model = "deepseek-v4.1-flash"` 原样保留 |
| 12 | pid 文件自愈 | pid 文件记的是已死进程、端口实际由另一个实例服务时，安装脚本与健康检查都会按端口占用者修正 |
| 13 | Cline 接入：目录解析 | `codex debug models` `exit=0`、2 个模型（两个上游各一条），含 `deepseek-v4.1-flash-cline` |
| 14 | Cline 接入：端到端工具调用 | `codex exec -m deepseek-v4.1-flash-cline` 执行 `echo` 并回读 `CLINE-E2E-OK`；路由器日志 `翻译转发 -> https://api.cline.bot/api/v1/chat/completions` |
| 15 | Cline 接入：工具图片走 chat 翻译 | 从 rollout 坐实：`function_call view_image` + 工具返回含 `input_image`；模型对纯绿图正确答出「绿色」 |
| 16 | 翻译层 Cline 方言适配 | 单元测试 `TestClineWireCompat`（7 项）覆盖 `reasoning` 字段、`reasoning_details` 数组、`data` 包装解包、Cline 形状的流式工具调用；全套 **41 项通过** |
| 17 | 全量健康检查 | **通过 19，失败 0，警告 0**，2 个模型端到端全部 `completed`（路由表从 7 条精简到 2 条后检查项相应减少） |

失败复现基线：修复前 Codex 的实际报错（路由器日志原文，模型与用户截图一致）
```
2026-09-12T18:14:29+0800 ERROR 上游拒绝 {"model":"deepseek-v4.1-flash",
  "error":{... "message":"Error from provider (Console Go): Upstream request failed:
  [invalid_request_error] Failed to deserialize the JSON body into the target type:
  input: missing field `call_id` at ..."}}
```

复现命令：

```bash
python tests/test_translate.py
python probe-upstreams.py --only deepseek-v4.1-flash
powershell -ExecutionPolicy Bypass -File check-model-router.ps1
```

---

## 9. 排查手册

| 现象 | 先看哪 |
|---|---|
| **界面弹「完成 Windows 设置 · Windows 安装未完成 · config_load」** | 这是**目录文件把 Codex 配置加载搞挂了**，不是沙箱权限问题。`codex debug models` 会直接打印解析错误（`unknown variant` / `duplicate field`）。检查 `model-catalog.json` 的 `visibility` 是否只用了 `list`/`hide`/`none`、有没有重复 JSON 键 |
| 目录解析报 `unknown variant` | `visibility` 写成了 `hidden` 之类。合法值只有 `list`/`hide`/`none` |
| 目录解析报 `duplicate field` | 手改 JSON 时写重了键。Python 的 json 不报错但 Rust serde 会拒绝，用 `check-model-router.ps1` 或重跑 build 覆盖 |
| Codex 报 `502 Bad Gateway`，url 是 `http://127.0.0.1:8791/...` | **不是上游问题，是路由器没在跑**。`python install-model-router.py --status`；`~/.codex/model-router/router.out.log` 有无 traceback |
| 502 且日志无 traceback、进程凭空消失 | 路由器被父进程的 Job Object 连带杀掉。确认启动带了 `CREATE_BREAKAWAY_FROM_JOB`（install 脚本已默认带） |
| 上游报 `missing field 'call_id'` | Codex 写出的工具条目缺失 `call_id`。看路由器日志有没有 `WARN 请求归一化`；老版本代码没有这个归一化，升级即修 |
| 上游报 `No tool call found for tool output with call_id ...` | 出现了有 `call_id` 但无配对调用的输出条目，归一化会把它降级成 user 消息 |
| 「选择模型」里没有新模型 | `codex debug models` 是否列出；`model_catalog_json` 路径是否存在 |
| 用量条不显示 | `[desktop] show-context-window-usage = true` 是否在；目录里该模型 `context_window` 是否 > 0（没分母不显示） |
| 请求 400 `MissingSessionID` | 路由表该条的 `session_header` 是否配了 `x-opencode-session` |
| 请求 403 | 上游 Cloudflare：确认 UA 是浏览器 UA（路由器默认已带） |
| 请求 400 `Invalid input`（chat 通道） | 工具结果里混了图片：查日志有没有 `split N image(s) out of tool result` |
| 400 `reasoning_content` 相关 | wire=chat 的多轮工具续聊：`ReasoningCache` 是否命中（重启路由器会导致未命中，但有占位兜底不应 400） |
| 返回 `incomplete` 而不是 `completed` | 思考模型把 `max_output_tokens` 烧在推理上了。要么加大预算，要么传 `reasoning.effort = "none"` |
| 400 档位非法 | 请求的 `reasoning.effort` 不在白名单：路由器会夹到最近合法档位 |
| `--status` 显示健康但 pid 不对 | pid 文件是脏的（手工启动的实例不写它）。安装脚本与健康检查都会按端口占用者自动修正 |
| 路由器无响应 | `python router.py --selftest`；再看 `router.out.log` |
