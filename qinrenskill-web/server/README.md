# 记忆系统 Web 服务（单 HTML + API）

## 作用（给非技术同事）

浏览器里仍然只有一个页面 `记忆系统MVP_1.html`，但**必须**通过本服务以 `http(s)://域名/` 打开（不能再用 `file://` 直接双击文件，否则无法调用同源的 `/api/...`）。

流程简述：

1. **左栏（一键）**：上传小说 txt + 人物名 + **API Key**，可触发服务端 **完整流水线**（抽片段 → 多批归纳 → 档案 → 扮演指令），用 `GET /api/novel/build/{job_id}` **轮询进度**，完成后自动创建会话并进入对话（扣 DeepSeek API，大书可能很慢）。
2. **左栏（仅片段）**：同一栏可只下载 **《角色片段》.md**，不调书内多批 LLM。
3. **中栏（省 API）**：将 md 交给 **DeepSeek 网页版** + 技能链得到扮演提示词，再粘贴回本页创建会话（**API Key 在左栏填写**）。
4. 聊天由本站 **代调** `/api/chat`（system 为会话内扮演的指令）。可选开启 `CHAT_DIALOGUE_SANITIZE=1`，对每轮助手回复做二次清洗，仅保留「说出的话」（见下表）。  
5. **朗读（可选）**：页面勾选「助手回复后朗读」且服务端配置火山 HTTP TTS 后，会 `POST /api/tts/volc` 用 [豆包 OpenSpeech V1 HTTP](https://www.volcengine.com/docs/6561/79820?lang=zh) 合成音频（与流式文档 [1329505](https://www.volcengine.com/docs/6561/1329505?lang=zh) 不同，本项目为非流式整段合成）。双向流式可后续再接。

## 本地启动

在仓库根目录：

```powershell
pip install -r requirements-server.txt
uvicorn server.main:app --host 0.0.0.0 --port 8000
```

可选：在仓库根目录复制 `.env.example` 为 **`.env`**，填写 `DASHSCOPE_API_KEY`、`WAN_S2V_PUBLIC_BASE_URL` 等；启动时 `server/main.py` 会自动 `load_dotenv`。**`.env` 已列入 `.gitignore`，勿提交。**

浏览器打开：`http://127.0.0.1:8000/`

## 公网部署要点

- **务必 HTTPS**：在反代（Nginx / Caddy / 云负载均衡）上终止 TLS。
- 启用 Cookie 安全标记（HTTPS 环境下）：

```text
set COOKIE_SECURE=1
```

（Linux/macOS 可用 `export COOKIE_SECURE=1`）

- **上传大小**：小说 txt 默认最大约 50MB（`MAX_UPLOAD_MB`）；片段为临时目录生成后立即下载响应。
- **粘贴长度**：扮演指令+规则默认合计上限约 25 万字符（`PASTED_PROMPT_MAX_CHARS`）。
- **会话存内存**：进程重启会丢会话；多实例需要改为 Redis 等共享存储（当前未实现）。
- **不要在日志中打印**：用户 Key、小说正文、扮演指令全文（当前代码仅返回错误码/简短 detail）。

## 环境变量（可选）

| 变量 | 默认 | 说明 |
|------|------|------|
| `MAX_UPLOAD_MB` | 50 | 上传小说 txt 大小上限 |
| `SNIPPET_FILE_PER_MINUTE` | 12 | 每 IP 每分钟大致允许生成片段文件次数 |
| `BUILD_PER_MINUTE` | 4 | 每 IP 每分钟大致允许 `POST /api/novel/build` 一键生成次数 |
| `BUILD_MAX_WORKERS` | 2 | 后台跑流水线的线程池大小 |
| `PIPELINE_MODEL` | deepseek-reasoner | 一键生成流水线用模型（可改为 `deepseek-chat` 等以降本） |
| `SNIPPET_CONTEXT_LINES` | 20 | 片段抽取时每侧上下文行数（与 `app.pipeline.run_snippet_extractor` 一致） |
| `SESSION_TTL_SECONDS` | 86400 | 会话有效期（秒） |
| `SESSION_COOKIE_NAME` | qr_session | Cookie 名 |
| `COOKIE_SECURE` | 0 | HTTPS 下设为 1 |
| `CHAT_MODEL` | deepseek-chat | 对话用 |
| `CHAT_DIALOGUE_SANITIZE` | 0 | 设为 `1` 时，每轮 `/api/chat` 在角色回复后再调一次 API，**只保留台词**（去动作/神态/旁白）；每轮多一次计费与延迟 |
| `CHAT_SANITIZE_MODEL` | deepseek-chat | 上述清洗步骤使用的模型 |
| `CHAT_SANITIZE_MAX_TOKENS` | 1800 | 清洗步骤 `max_tokens` |
| `VOLC_TTS_APP_ID` | （空） | 火山豆包语音应用 appid，与 `VOLC_TTS_ACCESS_TOKEN` 同时配置才启用 `/api/tts/volc` |
| `VOLC_TTS_ACCESS_TOKEN` | （空） | 控制台获取的 access_token |
| `VOLC_TTS_CLUSTER` | volcano_tts | 与控制台集群一致 |
| `VOLC_TTS_VOICE_TYPE` | BV102_streaming | 音色代码，以控制台为准 |
| `VOLC_TTS_ENCODING` | mp3 | 如 `mp3`、`wav` |
| `VOLC_TTS_URL` | https://openspeech.bytedance.com/api/v1/tts | 一般无需改 |
| `VOLC_TTS_UID` | qinrenskill_web | 请求体 `user.uid` |
| `VOLC_TTS_SPEED_RATIO` / `VOLUME_RATIO` / `PITCH_RATIO` | 1 | 语速/音量/音高 |
| `TTS_PER_MINUTE` | 30 | 每 IP 每分钟大致允许朗读次数 |
| `PASTE_PER_MINUTE` | 20 | 每 IP 每分钟大致允许 `/api/session/paste` 次数 |
| `PASTED_PROMPT_MAX_CHARS` | 250000 | 粘贴的扮演指令+补充规则总字符上限 |
| `PASTED_PROMPT_MIN_CHARS` | 20 | 粘贴内容最短字符数 |
| `QUICK_PER_MINUTE` | 30 | 每 IP 每分钟大致允许 `/api/session/quick` 次数 |
| `MAX_CHAT_MESSAGES` | 40 | 单会话保留的最大消息轮次（user+assistant） |

## 一键生成 API（轮询）

| 方法 | 路径 | 说明 |
|------|------|------|
| `POST` | `/api/novel/build` | `multipart`：`novel`（.txt）、`character_name`、`aliases_text`、`api_key`、可选 `chat_rules`；返回 `{ ok, job_id }` |
| `GET` | `/api/novel/build/{job_id}` | `{ done, error, message, percent }`；成功且 `done` 时含 `system_prompt` |
| `POST` | `/api/tts/volc` | JSON `{"text":"..."}`（≤3000 字）；需会话 Cookie；返回 `audio/mpeg`（或依 `VOLC_TTS_ENCODING`） |

## 数字人视频 MVP（阿里云 wan2.2-s2v）

对话页折叠区「数字人视频 MVP」：上传肖像图 + **口播文案**（或仅填 **AI 生成台词提示**）→ 本站用会话内扮演稿做 **一次性 LLM**（不写入对话历史）→ **火山 TTS** → 将图/音挂到 **公网可访问 URL** 供阿里云拉取 → **图像检测** → **异步生成视频** → 前端轮询 `job` 接口并在 `<video>` 中播放结果链接。

**地域与计费**：须使用 [阿里云百炼](https://bailian.console.aliyun.com/) **中国内地（北京）** 的 API Key；按官方文档对 **检测** 与 **视频秒数** 计费；`wan2.2-s2v` **同时仅 1 个进行中任务**，本站同一时刻也只放行一路流水线。

**公网 URL（必填）**：DashScope 只接受公网 `image_url` / `audio_url`。请配置 **`WAN_S2V_PUBLIC_BASE_URL`** 为外网访问本站的根地址（无尾斜杠），例如 `https://yourdomain.com`，并保证阿里云能访问  
`/api/mvp/wan-s2v/asset/{token}/image` 与 `.../audio`（该路径**无 Cookie**，仅用于云端拉取；token 为随机且任务结束后删除）。

**音频时长**：官方要求驱动音频 **短于 20 秒**；口播字数默认上限见 `WAN_S2V_MAX_SPOKEN_CHARS`（默认 400），过长请自行缩短。

| 变量 | 默认 | 说明 |
|------|------|------|
| `DASHSCOPE_API_KEY` | （空） | 北京地域百炼 API Key；未配置则接口返回 503 |
| `WAN_S2V_PUBLIC_BASE_URL` | （空） | 公网根 URL，未配置则无法提交任务 |
| `WAN_S2V_STYLE` | （空） | 可选，传入 `wan2.2-s2v` 的 `parameters.style`（如文档中的 `speech`）；请求里也可传 `style` 表单字段覆盖 |
| `DASHSCOPE_BASE_URL` | `https://dashscope.aliyuncs.com` | 一般无需改 |
| `WAN_S2V_MAX_SPOKEN_CHARS` | 400 | 口播最大字数 |
| `WAN_S2V_POLL_INTERVAL_S` | 15 | 查询任务间隔（秒） |
| `WAN_S2V_POLL_TIMEOUT_S` | 900 | 轮询最长等待（秒） |
| `WAN_S2V_START_PER_MINUTE` | 8 | 每 IP 每分钟允许 `POST .../start` 次数 |
| `WAN_S2V_IMAGE_MAX_MB` | 8 | 上传肖像最大体积 |

| 方法 | 路径 | 说明 |
|------|------|------|
| `POST` | `/api/mvp/wan-s2v/start` | `multipart`：`image`（必填）、`spoken_text`（有则最优先）、`llm_prompt`（次优先；会调 LLM 写台词）、`use_last_assistant_reply`（默认 `1`：用会话里最后一条助手回复作口播，再走火山 TTS）、`resolution`、`style`、可选 `volc_*`；需会话 Cookie；返回 `{ job_id, spoken_text }` |
| `GET` | `/api/mvp/wan-s2v/job/{job_id}` | `{ status, message, video_url, error, ... }` |
| `GET` | `/api/mvp/wan-s2v/asset/{token}/image` | 公网可读，供阿里云拉图 |
| `GET` | `/api/mvp/wan-s2v/asset/{token}/audio` | 公网可读，供阿里云拉音频 |

## 限流与安全说明

- `SNIPPET_FILE_PER_MINUTE` / `PASTE_PER_MINUTE` / `QUICK_PER_MINUTE` 为内存计数，服务重启清零；公网建议再加 WAF / 云厂商限流。
- 用户仍可在浏览器里看到**自己的对话与已粘贴的扮演稿**；**不能**防止用户用抓包查看自己的 HTTPS 流量。

## Docker 部署（推荐）

仓库根目录已提供 `Dockerfile` 与 `docker-compose.yml`，镜像内已包含：**小说一键流水线**所需的 `app/`、`server/`、`scripts/extract_character_snippets.py`，以及 `.cursor/skills/.../templates/` 下的两个模板文件（见 `app/pipeline.py` 路径约定）。

```powershell
# 构建
docker build -t qinrenskill-memory .

# 运行（本机试访问 http://127.0.0.1:8000/）
docker run -d -p 8000:8000 -e COOKIE_SECURE=0 --name memory qinrenskill-memory

# 或使用 Compose（同上）
docker compose up -d
```

公网 **HTTPS** 反代后面的容器请设置 `COOKIE_SECURE=1`（或写入 `docker-compose.yml` 的 `environment`）。

**仅使用「小说提取」三栏时**：打开站点根路径 `/` 即可；`/lover` 为恋人问卷（可选，不访问即不使用）。

## PyInstaller / exe

本服务为 **Python Web**，不推荐强行打成单 exe；公网请用进程管理（systemd、Docker、云函数镜像等）托管 `uvicorn`。
