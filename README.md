# 主播歌切播放器（songcuts）

从主播直播录播中**自动提取完整唱歌片段（歌切）**并**识别歌名**的一站式工具，提供网页播放器、后台管理、B 站投稿与 BililiveRecorder 全自动联动。

针对"伴奏翻唱识别不到"、"前奏尾奏被切"、"说话时放的 BGM 被当成歌"、"串烧多首歌挤在一个文件里"这些常见痛点做了完整的工程化处理，全部能力在 131 分钟真实录播上实测验证。

## 功能总览

| 能力 | 说明 |
|---|---|
| 歌切提取 | 从整场录播自动切出完整唱段，前奏/尾奏自动扩展（能量回扫，遇说话/静音停止） |
| 歌曲识别（三层） | ① ACRCloud 音频指纹（原曲在放的场景）② 歌词识别（Whisper 转写 + 网易云歌词搜索，覆盖伴奏翻唱）③ 后台人工改名兜底 |
| 边界对齐 | 利用指纹返回的原曲位置/时长反推歌曲真实起止，自动重新剪辑补全被切的前奏/尾奏 |
| 串烧拆分 | 多首歌连唱的大块按指纹偏移聚类自动拆成单首歌文件 |
| 真唱门控 | 用能量统计（持续活跃段/波动系数）区分"主播在唱"和"说话+放 BGM"，BGM 不再被误当成歌切 |
| 重叠片段合并 | 歌中长间奏导致的重复/重叠切片自动融合为一个连续片段 |
| 自动化 | 对接 BililiveRecorder Webhook：直播结束自动提取 → 识别 → 命名 → 清理原录播 |
| B 站投稿 | 对接 biliup：歌切转码投稿 B 站，标题/简介/标签模板化 |
| 合集发布 | 识别出的歌切一键批量投稿并归入 B 站合集（自动创建合集、复用已有合集、BV 号自动归档） |
| 播放器/后台 | 前台歌切播放器（分类/搜索），后台管理（提取参数、录播导入、投稿配置） |

## 系统架构

```
BililiveRecorder ──FileClosed Webhook──▶ FastAPI (main.py)
                                          │
                    ┌─────────────────────┼──────────────────────┐
                    ▼                     ▼                      ▼
          songcut_extractor.py   songcut_automation.py   bili_upload_integration.py
          （提取：classic 能量模式  （识别：指纹 → 歌词 →    （biliup 投稿）
           / gpu-model 模型模式）    对齐/拆分/门控）
                    │                     │
                    ▼                     ▼
              ffmpeg/ffprobe      ACRCloud API / faster-whisper
                                   网易云歌词 API / lrclib
```

| 模块 | 职责 |
|---|---|
| `main.py` | FastAPI 应用：提取/识别/管理/投稿全部 API 与后台页面 |
| `songcut_extractor.py` | classic 能量提取（纯 Python + ffmpeg），边界扩展、区域能量测量、片段导出 |
| `gpu_songcut_extractor.py` | inaSpeechSegmenter 模型提取（TensorFlow GPU），分块重叠、music 占比过滤、边界精修 |
| `songcut_automation.py` | ACRCloud 指纹识别、指纹边界对齐、串烧拆分、真唱门控、Whisper 歌词识别、识别缓存 |
| `brec_integration.py` | BililiveRecorder API/Webhook 接入与录播扫描 |
| `bili_upload_integration.py` | biliup 命令行封装与 B 站投稿配置 |
| `bili_season_integration.py` | B 站合集管理（创作中心 API）：列表/创建/归档视频、批量发布编排 |
| `demucs_runner.py` | Demucs 人声分离运行器（含短输入补丁，供扩展使用） |
| `static/` `admin_views/` `private_views/` | 前台播放器与管理后台页面 |

## 快速开始

### Docker 部署（推荐）

```bash
# 1. 准备配置
cp .env.example .env
#    编辑 .env：设置唯一的 UPLOAD_PASSWORD；如需指纹识别，再设置 ACRCLOUD_* 三项

#    B 站 cookies.json 请在后台上传，或放入 data/app/bili_cookies/；不要提交到 Git

# 2. 启动（含 BililiveRecorder）
docker compose up -d --build

# 3. 访问
#    播放器:  http://127.0.0.1:8000/
#    后台:    http://127.0.0.1:8000/admin/login
```

首次使用歌词识别时容器会自动下载 Whisper 模型（medium 约 1.5GB，缓存在 `data/app/hf_cache`，只下载一次）。

仓库只提供 `.bili_upload_config.example.json` 和 `cookies.example.json`。实际的 B 站 Cookie、API
密钥、登录密码和二维码均属于本地凭据，已由 `.gitignore` 排除。

### GPU 部署（inaSpeechSegmenter 模型提取）

```bash
docker compose -f docker-compose.yml -f docker-compose.gpu.yml up -d --build
```

GPU 镜像额外提供 `gpu-model` 提取模式（TensorFlow + inaSpeechSegmenter，语音/音乐标签分类）。

### 本地开发

```bash
python -m venv .venv && .venv/Scripts/pip install -r requirements.txt   # Windows
# 或 pip install -r requirements.txt

uvicorn main:app --host 0.0.0.0 --port 8000
```

依赖：Python 3.11+、系统 `ffmpeg`（或在后台手动指定路径）。

## 配置说明

所有配置通过环境变量（`.env`）注入。登录密码和外部服务凭据没有仓库内默认值，部署前必须自行设置。

### 基础

| 变量 | 默认 | 说明 |
|---|---|---|
| `ADMIN_USERNAME` / `UPLOAD_PASSWORD` | admin / 必填 | 后台登录与 API 密钥（`X-API-Key`） |
| `BREC_DEFAULT_AUTO_CATEGORY` | 录播姬自动提取 | 自动提取的歌切分类名 |
| `SONG_RECOGNITION_CACHE_PATH` | .song_recognition_cache.json | 识别结果缓存（按文件内容哈希） |

### ACRCloud 指纹识别

| 变量 | 默认 | 说明 |
|---|---|---|
| `ACRCLOUD_HOST` / `ACRCLOUD_ACCESS_KEY` / `ACRCLOUD_ACCESS_SECRET` | 空 | ACRCloud 凭据，三项齐全才启用指纹识别 |
| `ACRCLOUD_SAMPLE_DURATION_SECONDS` | 12 | 单个采样点时长（8-12s） |
| `ACRCLOUD_MAX_SAMPLES` | 8 | 首轮采样点数上限 |
| `ACRCLOUD_MIN_CONFIRMATIONS` | 2 | 接受结果需要的最少一致采样数 |
| `ACRCLOUD_MIN_CONFIDENCE` / `ACRCLOUD_HIGH_CONFIDENCE` | 0.68 / 0.90 | 多样本平均/单样本接受的置信度阈值 |
| `ACRCLOUD_RETRY_ENABLED` / `ACRCLOUD_RETRY_MAX_SAMPLES` / `ACRCLOUD_RETRY_GRID_SECONDS` | true / 6 / 45 | 首轮失败后的第二遍网格补采样 |
| `ACRCLOUD_ENERGY_SAMPLE_ENABLED` 等 | 见 .env.example | 能量优选采样点位置 |

### 边界对齐与串烧（指纹驱动）

| 变量 | 默认 | 说明 |
|---|---|---|
| `SONGCUT_BOUNDARY_ALIGNMENT_ENABLED` | true | 利用指纹偏移自动重剪补全前奏/尾奏 |
| `SONGCUT_ALIGNMENT_MAX_EXTEND_START_SECONDS` | 90 | 起点最多前扩（补前奏） |
| `SONGCUT_ALIGNMENT_MAX_EXTEND_END_SECONDS` | 30 | 终点最多后扩（补尾奏） |
| `SONGCUT_ALIGNMENT_MAX_SHRINK_END_SECONDS` | 10 | 终点最多收缩（直播翻唱常比录音室版长，收缩过狠会切尾奏） |
| `SONGCUT_MEDLEY_SPLIT_ENABLED` | true | 多首歌长块自动拆分 |
| `SONGCUT_ALIGNMENT_*` 其余 | 见 .env.example | 一致性容忍、原曲时长合理区间等护栏 |

### 真唱门控（防 BGM 误判）

| 变量 | 默认 | 说明 |
|---|---|---|
| `SONGCUT_MIN_SUSTAINED_ACTIVE_SECONDS` | 10 | 判真唱所需的最长持续活跃段（实测真唱≥14s，说话+BGM 只有 5-6s） |
| `SONGCUT_MAX_ACTIVITY_CV` | 0.62 | 持续段不足时允许的能量波动系数上限 |
| `SONGCUT_MIN_ACTIVITY_RATIO` | 0.35 | 持续段不足时要求的最低活跃占比 |

### 歌词识别（伴奏翻唱）

| 变量 | 默认 | 说明 |
|---|---|---|
| `SONGCUT_LYRIC_RECOGNITION_ENABLED` | true | 指纹识别失败后的歌词回退开关 |
| `SONGCUT_WHISPER_MODEL` | medium | Whisper 模型（small 更快、medium 中日文唱腔明显更准） |
| `SONGCUT_WHISPER_DEVICE` | auto | auto 时探测 CUDA 可用性（带 60s 自检探针），不可用自动回退 CPU |
| `SONGCUT_LYRIC_MIN_MATCH` | 0.55 | 转写在候选歌词中的最低包含度 |
| `SONGCUT_LYRIC_MAX_WINDOWS` / `SONGCUT_LYRIC_WINDOW_SECONDS` | 4 / 45 | 转写窗口数量与单窗时长 |
| `SONGCUT_NETEASE_API_BASE` | https://music.163.com | 歌词正文检索引擎（网易云 type=1006） |
| `SONGCUT_LRCLIB_BASE_URL` | https://lrclib.net | 备用歌词库（按歌名检索） |

### 提取相关（classic 模式）

| 变量 | 默认 | 说明 |
|---|---|---|
| `SONGCUT_INTRO_SEARCH_SECONDS` / `SONGCUT_OUTRO_SEARCH_SECONDS` | 30 / 45 | 前奏/尾奏能量回扫的最大范围 |
| `SONGCUT_INTRO_SILENCE_SECONDS` / `SONGCUT_OUTRO_SILENCE_SECONDS` | 4 | 回扫停止所需的持续静音时长 |

其余（提取时长、边距、活跃比等）在后台管理页按次调整；GPU 模式专属参数（分块重叠、music 占比阈值、noEnergy 桥接）见 `.env.example`。

## 歌曲识别管线详解

识别按三层顺序回退，全自动：

```
歌切片段
   │
   ▼
① ACRCloud 指纹识别（8-14 个采样点 + 多样本投票）
   │  匹配到 → 真唱门控（能量统计）
   │            ├─ 真唱特征 → 边界对齐/串烧拆分 → 以「歌名 - 艺人」命名
   │            └─ 说话+BGM 特征 → 保留时间戳命名，元数据记 bgm_suspected
   │  未匹配
   ▼
② 歌词识别（仅对具真唱特征的片段）
   │  Whisper 转写（VAD 回退防误杀唱腔）
   │  → 转写拆短片段 + 剥离幻觉标记
   │  → 网易云歌词正文检索（对错字容错）
   │  → 全段转写在候选歌词中的包含度 ≥ 0.55 才接受
   │  → 翻唱版自动修正为原唱艺人 → 命名
   │  未匹配
   ▼
③ 人工兜底：后台试听后手动重命名
```

**关键设计说明**

- **指纹识别的原理边界**：音频指纹匹配的是录音室原曲的声学特征。原曲在直播中播放（跟着原曲唱、纯放歌/BGM）时能命中；**伴奏/卡拉OK 翻唱与清唱原理上无法命中**——这正是歌词识别层存在的意义。
- **为什么需要真唱门控**：指纹对"说话时放的 BGM"同样会命中（天天、Heatstroke 实测案例）。能量统计可区分两者：真唱有 ≥14s 的持续响亮段（长音+连续伴奏），说话+BGM 的响亮段只有 5-6 秒且波动大。
- **为什么终点只小幅度收缩**：直播翻唱普遍比录音室版长（唱得慢、加词、段间说话），按原曲时长收缩 30s 会切掉尾奏（实测教训），故上限收紧为 10s。

## API 一览

管理接口需登录后台或携带 `X-API-Key: <UPLOAD_PASSWORD>`。

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/songcuts` | 歌切列表（`category`/`search` 过滤） |
| GET | `/api/songcut-categories` | 分类列表 |
| POST | `/api/songcuts/extract` | 上传录播文件手动提取（表单参数：模式/时长/边距等） |
| GET | `/api/songcuts/extractor-info` | ffmpeg/识别/清理/投稿状态诊断 |
| POST | `/api/brec/webhook` | BililiveRecorder 事件回调（FileClosed 触发自动提取） |
| GET | `/api/brec/summary` | 录播姬配置/房间/最近录播 |
| POST | `/api/brec/config`, `/api/brec/import` | 保存录播姬配置、手动导入指定录播 |
| POST | `/api/bili-upload/upload` | 单个歌切投稿（biliup） |
| GET | `/api/bili-upload/seasons` | 列出账号下已有的 B 站合集 |
| POST | `/api/bili-upload/publish-collection` | 批量投稿勾选的歌切并归入同一合集（`paths` + `season_title`/`season_id`，不存在时自动创建，封面取首个视频） |
| GET | `/api/audio-list` `、`/api/categories | 原有音频库（分类短音频） |

提取接口返回体包含每个片段的 `start/end`、识别结果、`aligned_by`、`alignment_shift_*`（对齐修正量）与顶层 `recognized_count`/`aligned_count`，便于观察识别与对齐效果。

## 管理后台

`/admin/login` 登录后（`/admin/songcuts`）：

- 手动上传录播提取（classic / gpu-model 模式、时长/边距/输出格式参数）
- 录播姬接入配置（API 地址、自动提取开关、Webhook 地址展示）
- 歌切库管理（试听、重命名——未识别片段的人工兜底入口）
- B 站投稿配置与触发
- 合集发布：歌切列表勾选（含"全选已识别"），选择已有合集或按标题新建，一键批量投稿并归档

## 测试

```bash
python -m unittest discover -s tests
```

75 项单元测试覆盖：边界扩展与间奏合并、GPU 分块重叠与跨块仲裁、music 占比过滤、识别分组归一化、指纹对齐数学与护栏、串烧拆分、真唱门控（真实录播测量值回归）、歌词打分/检索/入口。全部离线运行（网络与模型调用打桩）。

## 性能实测参考（131 分钟录播、CPU only）

| 阶段 | 耗时 |
|---|---|
| 能量分析（classic） | 88 秒 |
| ACRCloud 指纹（16 段 × 8-14 采样点） | 约 37 分钟（网络往返为主，可调低采样数换速度） |
| 歌词识别（medium / CPU，含转写+检索） | 每段约 1-3 分钟 |

## 常见问题

**Q：为什么有的歌识别不出来？**
指纹层认不出伴奏翻唱（原理限制，走歌词层）；歌词层依赖转写质量——中文容错好，日语唱腔用 small 模型可能转写太差，建议保持 medium。全识别失败的片段在后台人工命名即可。

**Q：Whisper 会不会在有显卡的机器上卡死？**
`SONGCUT_WHISPER_DEVICE=auto` 内置 60 秒 CUDA 自检探针（0.3 秒静音真实推理），检测到 CUDA 运行库缺失（如缺 cuBLAS）自动回退 CPU，不会挂死。

Windows + RTX 50 系列如果使用本地 `.venv`，还需要安装 `requirements-gpu.txt` 中的
cuBLAS/cuDNN 运行库；程序会自动注册 pip 安装的 NVIDIA DLL 目录，随后 faster-whisper
会在 `auto` 模式下使用 CUDA。旧的 `gpu-model` 分段器仍依赖 TensorFlow/inaSpeechSegmenter，
在尚未包含 compute capability 12.0 内核的 TensorFlow 版本上会失败；这种情况下可继续用
classic 快速能量分段，同时让歌词识别走 GPU Whisper。

**Q：清唱/很安静的翻唱被真唱门控拦了？**
调低 `SONGCUT_MIN_SUSTAINED_ACTIVE_SECONDS`（如 8）或 `SONGCUT_MAX_ACTIVITY_CV`（如 0.7）。

**Q：两首不同的歌被合并成一个片段？**
检查两首之间是否只有 <18s 的间隙（`merge_gap`），或把 `SONGCUT_INASEG_MIN_MUSIC_RATIO`（GPU 模式）调高。

**Q：识别结果会重复调用 API 吗？**
不会。识别结果按导出文件内容哈希缓存（`.song_recognition_cache.json`），同一文件重复处理直接命中缓存；旧格式缓存条目会自动重识别升级。

## 目录结构

```
├── main.py                     # FastAPI 应用与全部路由
├── songcut_extractor.py        # classic 能量提取 + 边界扩展 + 片段导出
├── gpu_songcut_extractor.py    # inaSpeechSegmenter GPU 提取
├── songcut_automation.py       # 三层识别管线（指纹/歌词/对齐/拆分/门控）
├── brec_integration.py         # BililiveRecorder 接入
├── bili_upload_integration.py  # B 站投稿
├── demucs_runner.py            # Demucs 人声分离运行器
├── static/                     # 前台播放器
├── admin_views/ private_views/ # 管理后台
├── tests/                      # 75 项单元测试
├── docker-compose.yml          # CPU 部署（含录播姬）
├── docker-compose.gpu.yml      # GPU 叠加配置
├── Dockerfile.gpu              # GPU 镜像（TF + inaSpeechSegmenter）
└── Caddyfile                   # 反向代理示例（Caddy, 8443 → 8000）
```

运行期产物不入库：歌切音频与元数据（`assets/songcuts/`）、识别缓存、处理清单等均已加入 `.gitignore`。

## 许可证

本项目原创源代码采用 **GNU General Public License v3.0 only** 发布，SPDX 标识为
`GPL-3.0-only`。

Copyright (C) 2025-2026 TianXyousa

完整条款见 [LICENSE](LICENSE)，项目授权范围见 [LICENSE-NOTICE.md](LICENSE-NOTICE.md)，
第三方组件许可见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。

仓库中的音频、录播、歌曲切片、模型权重、二维码、封面及其他第三方媒体不因与源码一同出现
而自动获得 GPL 授权；使用或再分发这些内容前，请确认你拥有相应权利。
