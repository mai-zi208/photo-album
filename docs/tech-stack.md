# 相册整理工具 · 技术选型决策

> 状态：已确认（2026-10-24）。平台 Windows 优先 / Python 内核 + Web UI / AI 混合（本地轻模型 + 云可选）/ 一期范围 = 地基版（索引 + 浏览 + 归位）。

## 1. 产品骨架（一句话）

接入（指定文件夹 / 持续监听）→ 建索引（缩略图 + EXIF + OCR，并行）→ 去重归组（pHash）→ 自动归位（时间/类型目录 + 文件名，预览 + 可撤销）→ 人机两层标识（自动信息为主，用户备注可选增强）→ 双出口（软件内搜索 / 资源管理器直接翻目录）→ 分享（单文件分享包，二期）。

设计原则：

- 目录是"索引的可重算投影"，只用不会出错的维度（时间、截图/照片类型、同日事件聚类）；语义标签进数据库不进目录。
- 机器先干完，人只做增益：用户备注是旁路，不卡主流程。
- 存量初始化与增量监听走同一条流水线；"可用"先于"完整"（元数据+缩略图先行，OCR 后台补齐）。

## 2. 技术栈总览

| 层 | 一期选择 | 说明 |
|---|---|---|
| 语言 | Python 3.11+ | 图像/AI 生态成熟 |
| 后端 | FastAPI（本地 127.0.0.1） | UI 通过 HTTP/WS 访问内核 |
| 数据库 | SQLite（FTS5 + sqlite-vec） | 单文件；二期语义向量复用同库 |
| 文件监听 | watchdog（Observer 设 daemon，导入方不卡退出） | 目录递归监听；事件只记"哪些路径脏了"，静默 2s 成批处理：先标消失路径 missing，再走同一条 ingest 对账（移动=同一行改路径，身份/时间不丢）；监听停摆期间的丢失由重扫 sweep 兜底 |
| 任务队列 | 后台线程 + ThreadPoolExecutor(4-8)（Pillow/blake3 在 C 路径释放 GIL，实测 6.8× 加速；进程池留作后手） | 入库分级：快通道（元数据/缩略图）与慢通道（OCR/去重） |
| 图像解码 | Pillow + pillow-heif（EXIF 走 Pillow `getexif()`；pilko 在 pip 源不可用，已弃用） | RAW 支持（rawpy）视需求后置 |
| 缩略图 | Pillow → WebP 两级（512px/128px） | 内容寻址目录 `thumbs/ab/<hash>.webp`；损坏图固定占位 |
| 哈希 | BLAKE3（内容主键）+ 自研 DCT pHash 64bit + numpy 汉明批量比对（近似去重，未引 imagehash） | 主键是哈希不是路径 |
| OCR | RapidOCR（ONNX Runtime，模型约 15MB） | 中文效果好；避开 Paddle 本体依赖 |
| 桌面壳 | pywebview（WebView2） | 后期可无痛换 Tauri，前端/sidecar 不动 |
| 前端 | Vite + Vue 3 + 虚拟滚动网格 | 3 万张级列表性能 |
| 打包 | PyInstaller + Inno Setup | Windows 安装包 |

## 3. 数据库草案

```sql
photos(
  id            TEXT PRIMARY KEY,   -- BLAKE3(content)
  path          TEXT,               -- 当前位置，可空（移动中对账）
  mtime, taken_at,                 -- 文件时间 / EXIF 时间（优先级：EXIF > 文件）
  width, height, bytes, mime,
  kind          TEXT,               -- screenshot | photo | other（规则判定）
  event_id      TEXT,               -- 同日 ± 分钟级/地理聚类
  caption       TEXT,               -- 二期：VLM 摘要（可云可本地）
  note          TEXT,               -- 用户备注（旁路，随时可补）
  dir_rule_ver  TEXT,               -- 当前所依目录规则版本，支持重排
  status        TEXT                -- indexed | ocr_pending | broken
);
CREATE VIRTUAL TABLE ocr_text USING fts5(text, id UNINDEXED);
tags(id, tag, source)                -- source: auto | user（仅建议，不做硬分类）
dup_group(group_id, id, distance)    -- pHash 聚类，一期只标记不删除
ops_journal(seq, ts, plan_json, state)  -- 归位计划：preview → applied → undoable
```

## 4. 归位（文件布局引擎）规则（一期）

- 目录结构：`<整理根>/<年份>/<YYYY-MM>_<事件名或日期>/<类型前缀>_<日期>_<AI描述或OCR摘要>.<ext>`；文件夹过碎自动并入上层。
- 两种模式：复制整理（原件不动）/ 原地重排；执行前必须输出预览（将移动 N 个文件 → 目标树示意），写入 ops_journal，支持一键撤销。
- 安全网：删除一律走回收站（send2trash）；跨目录移动用同盘 rename 优先。
- 对账：路径变化后按内容哈希重新挂接，用户手动挪动文件夹不破坏索引。

## 5. 分期

一期（地基版）：

- [x] 指定文件夹扫描入库（实测 44.7 张/秒，3 万张 ≈ 11 分钟）
- [x] 缩略图时间轴浏览（日期降序分组、懒加载、损坏图占位卡）
- [x] pHash 去重标记（2026-10-24 落地）：DCT pHash 在缩略图同一次解码中顺带计算（近零额外成本）；numpy 批量汉明距离 + 并查集全局归组，阈值 8/64；老库后台回填；UI 提供"重复 N"分组视图（组内最早标记为保留候选）与时间轴"重"角标。只标记不删除。已知取舍：同模板不同文字的截图（如通知/聊天界面）可能被判近似——这正是"近似"的含义，去留由用户在分组视图里目测定夺
- [x] 截图/照片/其他自动判定（一期为扩展名+EXIF 规则）
- [x] OCR 慢通道已落地（2026-10-24）：RapidOCR 单 worker，扫描完成后自动启动，截图优先处理；FTS5 + CJK 二元/一元切分支持中文子串搜索；实测文字截图识别 12/12 命中，约 0.8 张/秒（后台低优先级，不阻塞浏览）
- [x] 按时间目录归位（2026-10-24 落地）：预览→执行→撤销日志；复制/移动两模式；删除一律 send2trash；字节级 BLAKE3 验证通过（事件聚类目录留待增强）
- [x] watchdog 增量监听（2026-10-24 落地）：扫描完成后自动监听该目录、开机恢复监听；新增/移动/删除/恢复/原地修改/整目录改名六种事件全部通过 `tools/watch_check.py` 断言（移动保持同一行身份）；missing 行对时间轴/搜索/重复组不可见但保留待对账；监听没赶上的丢失由重扫 sweep 兜底
- [x] 用户备注字段与详情卡（2026-10-24 落地，一期收官）：note 列只进数据库不碰文件（旁路增强，内容哈希主键保证备注随文件移动存活）；详情卡 = 512 预览 + 全量元数据 + OCR 全文 + 近似重复提示 + 备注编辑（Ctrl+Enter 保存）；搜索合并备注命中（source: ocr | note | ocr+note），时间轴/结果卡带"注"角标；`tools/notes_check.py` 18 条断言全过 + 真实数据无头截图验证

一期全部完成。二期推进中：语义向量搜索 ✓ / VLM 描述 ✓ / 分享包 ✓ / "那年今日" ✓ / 手机相册式 UI 壳 ✓ / 视频与 GIF 播放管线 ✓ / 自动改名成内容名 ✓ / 图集页"添加图集"（收藏集）✓ / 精彩回忆动态视频（配乐即时播放 + ffmpeg 导出）✓ / 人物（本地人脸检测）✓ / GPS 地名反查（离线城市小表）✓。二期全部完成。

二期：

- [x] 语义向量搜索（2026-10-25 落地）：Chinese-CLIP ViT-B/16 ONNX int8 双塔（Xenova 转换版，hf-mirror 下载，`onnx.utils.extract_model` 拆成 vision/text 两个单塔图；无 torch，onnxruntime CPU 4 线程实测 3.9 张/秒，2034 张全库回填约 9 分钟零失败）。向量存 `sem_vec(id, dim, float32-blob)`，L2 归一后暴力余弦（3 万×512≈60MB，毫秒级，不需要 ANN 索引）；embedding 是第三条慢通道（扫描后/监听批次后自动回填，`kick_slow_lanes` 统一收口）。UI：搜索框"文字/语义/全部"引擎切换、结果卡相似度角标与来源徽标、详情卡"找相似"、`?q=&engine=semantic`、`?id=&similar=1` 深链。`tools/semantic_check.py` 10 断言全过（中文查询 top-1 正确、text 引擎不越权、以图搜图变体第一、监听新增免重扫即可语义检索）
- [x] VLM 自动图片描述·云端通道（2026-10-25 落地）：OpenAI 兼容 chat/completions 任意供应商（默认占位 DashScope+qwen-vl-plus，base_url/key/model 在顶栏"云端…"设置里改）。隐私按构造保证：默认关闭、key 只存本地 `.data/caption_config.json` 且 UI 回显永远打码、只上传 512px WebP 缩略图的 base64、截图默认不外传（全局勾选或详情卡"仅这张外传"force 单张放行）。触发=按需+新照片自动：存量照片 caption_status 一律 'off' 永不自动回填，详情卡按钮逐张生成；扫描/监听进来的新照片标 pending 由 `kick_slow_lanes` 排队消费。描述与备注同池子串检索（source 徽标"描述"，可与 文字/备注/语义 叠加），时间轴"述"角标。`tools/caption_check.py` 26 断言全过（本地 mock 端点：关闭拒发、key 掩码与留空保留、截图策略、force、搜索归因、watcher 自动 pending→done）+ 真实库无头截图验证设置弹窗/详情卡/搜索结果三处 UI。2026-10-25 已接真实阿里云 DashScope key（账号无 `qwen-vl-plus`，视觉模型名是 **`qwen3-vl-plus`**，404 model_not_found 排雷；真实合影照片描述精准读出横幅文字，截图默认拒发策略线上复验通过）
- [x] GPS → 地名反查（2026-10-25 落地）：用户选定形态=**离线小表·城市区级**（零网络、零模型、零第三方数据依赖，整条功能只有 `app/places.py` + 四个库列）。内置 ~170 座城市坐标（中国地级市为主 + 港澳台 + 少量海外常用目的地）；EXIF GPS IFD（0x8825）按 DMS 有理数 + N/S/E/W 转十进制，haversine 最近邻，**80 km 内取城市名、超出记"其他地点"**（城市级精度，不假装知道具体景点；上海↔苏州≈85 km 因此不会互相吞并）。photos 新增 `gps_lat/gps_lon/place/gps_status`；入库时 EXIF 已打开，读完即 `done`（视频入库即 `done`，海报帧没有相机 GPS）；**存量照片走第四条慢通道回填**——单 daemon 线程、只读文件头不解码像素（heic/avif 安全）、每 200 行提交、坏文件标 `failed` 不炸批次，`kick_slow_lanes` 统一收口。API：`GET /api/places`（with_gps/named/other/pending + 按城市分组的 `{place,count,cover}`）、`POST /api/places/scan`、`/api/photos?place=`（与 kind/album/has_face 等过滤叠加），详情响应直接带 `place` 与坐标。UI：发现页"人物与地点"的地点卡按城市（副标题"N 张"、封面取最新可看行；回填中 4s 自刷新），点开 `openAlbum("place=…")` 复用结果网格；预览栏在位置上方新增"地点"行；深链 `?view=place&name=北京`。`tools/places_check.py` 60 断言全过（**合成 GPS EXIF 测试图**：N/S/E/W 与 DMS 解析、北京/上海/悉尼/纽约命中、100 km 外与太平洋→其他地点、null island 拒收、入库落库、视频豁免、存量回填 pending→done + 坏文件 failed、重扫不丢、place 正反向过滤与分组封面、HTTP 表面）+ 无头截图验证 发现页地点卡/城市网格/预览栏地名/深链 四态。坑：Pillow 13 的 `Image.Exif().get_ifd(0x8825)` 可直接写 GPS IFD（IFDRational 可序列化），无需 piexif；GPS 空值常以 (0,0) 写入，解析时按"无定位"丢弃。
- [x] 分享包（2026-10-25 落地）：搜索结果一键"打包分享"→ 自包含单文件 HTML（`app/share.py` 构建，`.data/share/` 落盘，`/share/<name>` 只读分发，文件名随机不可覆盖，路径穿越 404）。零外部依赖零网络：512px WebP 缩略图 base64 内嵌 + JSON 数据岛 + 内联 CSS/JS；按天分组时间轴 + 灯箱（←/→/Esc）。隐私按省略保证：包里只有 缩略图字节/日期/类型 三样，不含原图、路径、文件名、OCR、备注、描述（页头明示"仅含缩略图 · 不含原图与文字信息"）；missing/无缩略图行自动跳过，2000 张上限。模板在 `web/share_template.html`。`tools/share_check.py` 25 断言全过（含 8 项泄漏金丝雀断言：把机密 OCR/备注/描述/路径种进库里再打包，逐串查无）+ 真实数据 12 张截图包无头截图验证两处 UI（`?q=&pack=1` 深链自动点按钮）
- [x] "那年今日"回顾（2026-10-25 落地）：`GET /api/onthisday[?date=MM-DD]`（默认今天）——SQL 按 `strftime('%m-%d', taken_at,'unixepoch','localtime')` 匹配且只取 `status='ok'`，服务层剔除今年、按年份降序分组（`{year, items, count}` + `year_span`）。前端：时间轴顶部提示条（有往年命中才出现，"那年今日 · 10月25日 · 来自 2 个往年 · 5 张，点击查看"），点开复用结果区渲染"XXXX 年的今天 · N 张"年份块（`.results-grid > .otd-group { grid-column: 1/-1 }` 通栏）；深链 `?view=onthisday[&date=MM-DD]`。零新依赖零后台通道（纯查询，复用缩略图与详情卡）。`tools/onthisday_check.py` 28 断言全过（跨年分组/组内时间倒序/今年剔除/missing 剔除/日期参数/6 种非法日期 400/默认=今天）+ 真实数据（今天恰有 2024×2 + 2025×3 命中）无头截图验证提示条与展开视图两处 UI

- [x] 手机相册式 UI 壳（2026-10-25 落地，按 docs/更好的想法.md 与 MIUI 参考图）：底部三标签 照片/图集/发现 + 顶栏固定搜索（时间/文件名/图中文字），全部零模型。照片页 年/月/日/我的照片 分段：`GET /api/grouped?level=year|month|day[&year&month]` 大封面卡（封面优先 status='ok' 行，同秒并列取可看的那张），年→月→日逐级下钻，日卡点开走 `GET /api/photos?level_key=`（同一端点支持 kind/ext/folder/status/date_from/indexed_from/order 过滤；folder 为前缀匹配，substr 而非 LIKE 绕开 Windows 路径转义，前端传值带尾分隔符防兄弟目录误入）；详情弹窗改为右侧资源管理器式预览栏（docked aside，窄屏转浮层）。图集页：`GET /api/albums` 智能图集（最近项目/相机/截屏录屏/GIF/相似照片/最近删除，纯路径与规则）+ 扫描根一级目录派生的文件夹图集；整理/重复/云端/扫描工具收进本页。发现页：精彩回忆（去年今日复用 /api/onthisday、年度回顾/近三个月/近一周走 grouped+date_from）、智能场景（kind/ext 规则）、人物与地点为说明卡（人脸模型/GPS 均在后续规划）。搜索合并 文件名（instr 子串）与 中文时间短语（"2025年3月21日"/"3月21日"→今年，年份限 19xx/20xx 防误解析）两个新来源，徽标"文件名/时间"。开机自动扫描 Windows「图片」库（FastAPI startup 钩子，TestClient 裸建不触发、scratch 测试隔离不受影响）。`tools/gallery_ui_check.py` 39 断言全过（分组封面择优/下钻过滤/前缀陷阱/分页不受影响/图集计数/时间短语解析）+ 真实库无头截图验证 年/月/日/预览栏/图集/发现/时间搜索 七个视图

- [x] 视频与 GIF 播放管线（2026-10-25 落地）：视频以**内容哈希同一等公民入库**（kind='video'，photos 新增 duration 列），扫描/监听走同一 process_file 分派；海报帧与元数据用 **imageio-ffmpeg 自带的 ffmpeg 7.1**（`app/media.py`：stderr 正则解析 Duration/Video 尺寸，`-ss` 定位取帧经 image2pipe PNG 管道回 PIL，取 10% 时长处失败回 0s；零系统依赖零转码），海报走既有 build_thumbs 双尺寸缩略图，取不到帧判 broken。视频**从源头豁免全部慢通道**：入库即 ocr_status/sem_status='done'（claim_pending 只吃 pending 故无需改查询）、不算 phash（dups 回填查询加 kind!='video'）、caption.on_ingested 见 video 直接返回。播放通路 **`GET /media/{id}` 原文件流**（FileResponse 天然支持 HTTP Range→206，拖动进度条可用；mp4/m4v/webm/mov 声明内联 type，mkv/avi 等冷门容器前端只显示海报+"用系统播放器打开原文件"；文件被删 404 不炸）。taken_at=mtime（EXIF 无拍摄时间可挖）。GIF 仍以静态图入库（缩略图/分享包/语义通道行为不变），预览栏改挂 `/media/{id}` 原文件播动画。前端：时间轴/结果卡视频=居中 ▶+右下时长角标、GIF=左上"动"角标；预览栏 `<video controls preload=metadata poster=512缩略图>`，切换条目与关栏都 pause+清 src 防后台续播；图集页"视频"智能卡（相机之后）+ 发现页智能场景"视频"。`tools/video_check.py` 43 断言全过（ffmpeg 生成的 mp4 入库字段/海报像素/慢通道豁免/损坏视频 broken/内容去重陷阱（同字节视频 dedup 到原行）/Range 206 与 content-range 精确校验/删除后 404/albums 视频卡/status 过滤/watch 增量收视频）+ 无头截图验证 时间轴角标/视频预览栏播放/GIF 动图/图集/发现 五视图。坑：`.dd-media` 的 grid+place-items:center 会把"打开原文件"链接叠进播放器中间，容器有 video 时需转 flex column

- [x] 自动改名成内容名（2026-10-25 落地）：把 IMG_2024xxxx / wx_image_0001 / 纯时间戳这类"机器名"改成**图片内容决定的名字**，名字来源 = 优先级链 **备注 > 云端描述(VLM) > 图中文字(OCR) 首行**，三者皆无则跳过（可选"类型_日期_id8"兜底，默认关）。`app/rename.py` 复用归位（layout）的 **plan→preview→apply→undo 日志安全模式**：计划先落 `.data/rename_plan.json`，执行逐步记结果、每 200 步持久化，撤销按日志逆序改回并把 DB path 指回原值。机器人名判定 `_ROBOTISH`：全数字、或字母开头**且以数字结尾**（`report`/`家庭聚餐合影` 保留，`wx_image_0001`/`IMG_2024_0506_112` 算乱名）；已有意义名（≥4 字符且非机器名）直接跳过并计数。清洗 `_KEEPER` 只留字母/数字/汉字/空格，去标点 emoji；stem 截 24 字符；同目录重名加 `-2`/`-3` 后缀；**只在原目录内改名不动目录结构**，目标已存在（用户后来放的）走 send2trash 回收。主键是内容哈希，改名只 UPDATE photos.path，id 不变 → 备注/OCR/向量/缩略图全部存活；watch 对人工改名按内容哈希对账不产生新行。隐私：caption 源只在用户已逐张开启云端描述的条目上生效，本地零上传。API 六端点 `/api/rename/{plan,current,preview,apply,apply_status,undo}`；前端图集页"改名…"按钮 → 对话框（旧名划线 → 新名 + 来源徽标 备注/描述/文字/兜底、待改名统计、进度条、撤销按钮），深链 `?view=rename`。`tools/rename_check.py` 44 断言全过（三来源各取一名/兜底开关/重名后缀/跳过有意义名/apply 后 DB 跟随且 id 不变/OCR 与文件名搜索改名后仍命中/undo 全量还原/watch 对账人工改名零新增行/API 表面）。坑：已命名检查必须**先于**无信号跳过，否则跳过计数漏掉真名字；测试里 apply 之后再按旧路径查 id 会全 None，扫描后要先冻结 id 映射；`-2` 后缀排序时 `-`(U+002D) 排在 `.`(U+002E) 前
- [x] 图集页"添加图集"·收藏集（2026-10-25 落地）：用户选定形态=**纯数据库收藏集**（图集只是库里的名字+内容哈希 id 集合，不建文件夹不搬文件——与"目录只按稳定维度组织、分类只进数据库"的核心决策一致），入口=**时间轴/结果卡 hover 复选框多选 + 详情预览栏"加入图集…"** 双路。`app/albums.py` + 两张表（albums 名字 UNIQUE、album_photos 复合主键天然去重），成员挂内容哈希主键故**改名/移动/重扫/撤销全不掉队**；删除图集只删两表行、照片零伤害。API：`/api/albums` 响应加 user 数组（含 count/cover，missing 成员不计入封面与计数），CRUD 六端点 + `/api/photos?album=`（_filter_sql 加 album 子查询，可与 kind 等叠加）+ `/api/albums_of/{id}`。前端：图集页"我的图集"区段+「＋ 添加图集」、用户卡 hover ⋯ 菜单（重命名 prompt/删除 confirm）、卡片点开 `openAlbum("album="+id)` 复用结果网格并显示"加入图集…"批量入口；多选模式=body.selecting + 底部悬浮胶囊条（已选 N/全选切换/取消），选择器对话框复选框即时 POST add/remove（单张模式预勾选当前所属、可反向移出）。`tools/albums_check.py` 43 断言全过（CRUD/重名拒/未知 id 静默忽略/幂等重加/封面取最新 ok 成员/missing 剔除/重扫成员存活/删图集不伤照片/路由顺序/过滤组合）+ 无头 iframe-shim 驱动真实点击六视图截图（含实跑一次"勾两张→勾选旅行 2026→API 计数 2→4"）。坑：自定义 `display:flex` 类会盖掉 `[hidden]` 属性（UA 规则 display:none 优先级最低），悬浮条需 `display:none !important` 兜底；按钮样式挂在 `.organize-dialog menu` 上，新对话框用 `div.od-menu` 会拿不到 danger/primary 配色

- [x] 精彩回忆动态视频（2026-10-25 落地）：用户选定形态=**即时播放 + 可选导出**、音乐=**本地合成循环**（项目完全离线，绝不拉在线曲库）。两条通路共享同一批素材：
  - 配乐：`tools/gen_music.py` 用 **numpy 离线合成** 3 段琶音循环（拨弦=1/2/3/4 次谐波×指数衰减包络，和弦进行 warm=C·G·Am·F / bright=Am·F·C·G 更快 / night=Dm·Bb·F·G 更慢，低八度叠一层错开 21ms，4 抽头延迟梳状"混响"+ tanh 软削波 + 首尾淡入淡出，手写 WAV 头再经自带 ffmpeg 转 96k AAC）落 `assets/music/{warm,bright,night}.m4a`（各 10–18s、约 0.15MB，循环无缝）。
  - 即时播放（前端，零等待零落盘）：结果网格头部"▶ 播放回忆"（≥2 张可看静图才出现，`showGrid`/`renderOnThisDay` 统一喂 `memoryItems`，剔除 broken 与 video）→ 全屏播放器：512 缩略图逐张 **CSS Ken Burns**（`kb-a`/`kb-b` 交替推拉，进场 `void offsetWidth` 重启动画）+ 0.9s 交叉淡入淡出、左上标题与"日期·备注/描述"字幕、底部进度点（可点跳转）、控制条 上一张/暂停/下一张/静音/当前曲名/**导出视频**/关闭；配乐随机抽一条 `<audio loop>`（autoplay 被拦仍照常放映，play() catch）。键盘 Esc/←/→/空格。
  - 可选导出（后端 ffmpeg）：`app/memories.py` `start_export` 后台线程 + 状态轮询（复用 rename/layout 的 apply 模式）。每图先渲染成一段 **zoompan Ken Burns** 片段（1280×720，fit+pad 不拉伸，`d=每段帧数`），再用 **xfade 链**交叉淡化拼接（offset 逐段累加 `acc+=seg-xfade`），末了 `-stream_loop -1` 循环配乐 + afade 淡出 mux 成 mp4；xfade 图失败自动回退 **concat demuxer 硬切**（保底出片）。源用已落盘 512 缩略图（必可解码、已 EXIF 转正、免碰 heic/avif），仅取可看静图、跳过 video，`max_photos` 默认 30 上限、each/xfade 夹紧。产物 `.data/memories/memory_<job>.mp4`，`cleanup_old` 定期回收。
  - API：`/api/memories/tracks`、`/api/memories/track/{name}`（白名单解析防穿越，FileResponse 支持 Range→206）、`POST /api/memories/export`（非法 id/空选择/未知曲名 400）、`/api/memories/status/{job}`（不回传 ids）、`/api/memories/file/{job}`（done 才发）。
  - 验证：`tools/memories_check.py` 40 断言全过（tracks 列表与探测时长/穿越拦截/源选择保序且剔 video+missing/坏曲名与空选择报错/**两次真机 ffmpeg 渲染**产出 1280×720、probe 时长≈计划、体积>40KB/HTTP 表面含音频 Range 206 与下载/cleanup）+ 无头 iframe-shim 真机截图两态（播放器打开含 Ken Burns 画面+字幕+进度点+控制条；导出态"渲染中 0/8…"进度与按钮禁用）。坑：`let memoryItems` 必须声明在底部 init 块之前否则同步调用路径 TDZ；`[hidden]` 播放器需 `display:none !important`；顶栏 `playMemoryBtn` 需顶层 const 供 showGrid 引用。

- [x] 人物·本地人脸检测（2026-10-25 落地）：用户确认形态=**只做"含人物的照片"图集，不做身份聚类/人脸建档**（不引入人脸 embedding、不做按人命名），整条通道只有一个轻量检测器，**零模型下载零上传**——OpenCV 的 Haar cascade XML 就打包在 `opencv-python-headless` 轮子里（钉 4.x，5.0 移除了 `CascadeClassifier`），正面 + 侧面两个 cascade 各自跑一遍再用 **IoU(0.3) 贪心合并**去重（三视角重复框保留大的）。检测直接跑在**已落盘 512px 缩略图**上（已 EXIF 转正、必可解码，不碰 heic/avif 原图），`minSize 28 / scale 1.15 / minNeighbors 5`。与 OCR/语义共用同一慢通道契约：新行 `face_status='pending'`、视频入库即 `'done'` 且 SQL 全查询豁免、`kick_slow_lanes` 统一收口、`app/people.py` 单 daemon 线程、解码失败标 `'failed'` 不炸批次、每 200 行提交一次；`face_count` 列（NULL=未扫、0=无人）随内容哈希主键存活，重扫/改名/移动不丢。API：`GET /api/people`（with_faces/faces/scanned/pending/available/progress）、`POST /api/people/scan`、`/api/photos?has_face=1|0`（`face_count>0` / `=0`，NULL 的视频自然排除）。UI：发现页"人物与地点"区段的人物卡（副标题"N 张含人物"/"识别中 N%"/"N 张待识别"；运行中 4s 轮询自刷新；无人物时可点击触发扫描），点开复用结果网格 `openAlbum("has_face=1")`，深链 `?view=people`。`tools/people_check.py` 33 断言全过（真机 cascade 加载 + 真检测跑通且空图=0、IoU 合并、pending→done、视频豁免、重扫不丢 face_count、has_face 正反向过滤、summary 口径、HTTP 表面）+ 无头截图验证 发现页人物卡/人物网格/预览栏/深链 四态。

- [x] 11 条修改意见落地（2026-10-25）：把应用从"工具集合"收敛成"手机相册式索引器"。(1) 开机自动索引 Windows「图片/桌面/下载/文档」（OneDrive 重定向同名目录按真实路径去重），增量 + watcher；(2) **所有操作都是索引操作**，真实文件可以位于任何位置，只有默认文件夹 `%USERPROFILE%\Pictures` 允许改动真实文件——`app/library.py` 的 `is_default_path` 闸门（junction/OneDrive 解析后再比较）；(3) 各页内置返回：详情栏→关闭、结果→上一级、日→月→年→我的照片、图集/发现→照片页，`navBackTarget` 单一出口；(4) 桌面底栏 tab 间距 56px（≤720px 回到 6px 手机式）；(5) 年/月/日/我的照片胶囊条 `position:fixed` 固定在底栏上方并水平居中；(6) 多选 8 操作——全选（当前视图全部 id）、类选（时间轴/种类/来源/主题，一整批替换选择，与全选天然互斥）、加入图集、删除（`trash_at` 索引进最近删除，不动文件）、移动（`/api/fs/list` 文件夹选择器 + `shutil.move`，非默认目录 skipped）、整理（默认文件夹内按 timeline/kind/source/scene 建子目录）、分享（原图 zip + `index.html`）、取消（退出选择）；最近删除在预览栏二次确认 `restore`/`purge`（默认目录 send2trash、其他目录只 `purged_at` 隐藏防重扫复活）；(7) 搜索合并为单一混合框（文字+语义+时间），绝对定位居中、固定顶栏；(8)(9) 图集页去「＋添加图集」、去整个「工具」区；最近项目/相机/视频/截屏录屏/GIF/最近删除同级，「更多图集」改按**下载来源**（`app/sources.py` 纯路径规则：微信/QQ/下载/相机/截图/桌面/文档/图片/其他）；(10) 去重改自动、扫描并入开机索引、云端描述 UI 移除（后端 caption 保留但不再有入口）；(11) 发现页顺序=人物与地点→智能场景→精彩回忆，智能场景改用 `app/scenes.py` 本地 Chinese-CLIP 零样本主题分类（风景/建筑/截图/文档/证书/动物/植物/交通工具/数码产品/卡通 + 其他，规则优先截图/文档/证书，MIN_SCORE=0.22，`scene_status` 可断点续跑）。API 新增 `/api/photo_ids`（当前视图全量 id）、`/api/fs/list`、`/api/photos/{trash,restore,purge,move,organize}`、`/api/share/zip`、`/api/scenes[/scan]`、`/api/sources`；`/api/albums` 返回 `{smart,sources,user}`；`/api/photos`、`/api/photo_ids` 支持 `source/scene/status=trashed` 过滤。`tools/library_check.py` 73 断言全过（来源分类 9 例、trash/restore/purge 默认与非默认分支、move 闸门、organize 四种模式、fs 选择器、zip 内容、HTTP 表面与 scenes 顺序），people_check 33 / places_check 60 回归全过，无头浏览器验收布局/图集/发现/选择栏/返回层级且 console 零报错。

- [x] 展示与多选交互打磨（2026-10-25 第二轮 5 条）：(1) **等比缩略图**——`.tile/.result-card/.level-card/.album-card` 的图片统一 `object-fit: contain` + 深色底（只缩放不裁剪，原比例完整显示）；(2) **名称只在查看时出现**——日视图卡片的 `title` 只留体积/尺寸/时长，卡片说明区不再回退文件名，完整路径仅在预览栏「位置」/详情中展示；(3) **时间轴折叠**——`.day` 前 10 天默认展开、其余带 `.collapsed`（日期标题 ▸/▾），点击标题切换，CSS `.day.collapsed:hover .grid{display:grid}` 实现鼠标停留自动展开；(4) **操作占用态状态机**——`beginOp/opClosed/cancelActiveOp` + `opSnapshot`：点操作按钮高亮占用，再点同一按钮取消；弹窗执行或关闭都回退到**进入操作前的选择**并保持多选，只有「取消」才清空并退出；删除成功后按“快照减去已删”保留剩余选择；移动弹窗改为仿资源管理器（标题栏/↑⌂地址栏/左侧快速访问/文件夹列表/状态栏），`/api/fs/list` 新增 `quick`（系统常用文件夹 + 磁盘根）；分享改为**先选导出目录、再点「确认生成」**，`ZipRequest.dest` + `share.build_zip(dest=…)` 写入用户目录并返回本地 `path`（目录不存在/不可写直接报错）；(5) **图集页顺序**——最近项目（智能卡 + 自建图集）→ 更多图集 → GIF → 最近删除，`#gifGrid`/`#trashGrid` 独立容器，自建图集仍归「最近项目」块。验证：`library_check` 78 断言（新增 share dest 三例 + fs quick）、people 33 / places 60 回归全过；无头浏览器逐项确认 contain、日视图无文件名、前 10 天展开/其后折叠且点击可切换、移动占用态与关闭回退、快速访问可切到 `F:\`、分享导出到指定目录产出 zip，console 零报错（CSS `:hover` 在无头环境无法触发，改以规则加载 + 合成点击验证）。

- [x] 展示与选择交互收尾（2026-10-25 第三轮 5 条，覆盖第二轮第 3 条）：(1) **撤销时间轴折叠**——删除 `OPEN_DAYS`/`openDayCount`/`.collapsed`/h2 点击监听与全部 `.day.collapsed*` CSS，按天分组恒展开、日期标题不可点；(2) **图集页「＋」卡**——去占位文字，`addAlbumCard()` 渲染虚线 `.add-album` 卡（`.add-icon` 显示「＋」），点击 prompt 新建后重渲染；(3) **按钮占用态统一**——`#selAddAlbum` 去掉 `primary`，走同一 `beginOp/opClosed/cancelActiveOp`；类选 tab 再点已选类别即 `cpDim=null` 并回「先选择一种类别。」，`renderClassList` 增加无类别短路；(4) **日视图纯缩略图**——`showGrid(..., {tiles:true})` 让 `#resultsGrid` 加 `.tiles` 并用 `makeTile` 渲染（与「我的照片」同款，无描述文字），`openDay` 改走该分支；(5) **真实图片与噪声清理**——`config.known_folders()` 读注册表 `User Shell Folders`（My Pictures/Desktop/Personal/Downloads）展开环境变量后供 `ingest.common_roots()` 与 `library.DEFAULT_ROOT` 使用（本机全部重定向到 `D:\图片`/`D:\桌面`/`D:\下载`/`D:\文档`）；`process_file` 跳过 OneDrive「仅在线」占位（`st_file_attributes & 0x400000` → skipped）与 macOS `._*` AppleDouble；`VIDEO_EXTS` 去掉 `.ts`（避免 TypeScript 源码被当视频）；`build_thumbs` 临时文件名带 pid+线程号（同内容并发不再互抢）；`db.get_conn` 不再逐连接切 `journal_mode`（WAL 是库级属性，只在 `init_db` 设一次），新增 `retry_locked` 给 `upsert_photo`/`scan_state`/`sweep_missing` 短退避重试，`watch._apply_batch` 在扫描进行中不启动慢通道。验证：清库后扫描 D:\图片等 4 个目录共 5491 张真实图片、坏图 2 张；`library_check` 78 / people 33 / places 60 全过；无头浏览器 4 项交互断言全 PASS。

## 6. 已知风险与对策

| 风险 | 对策 |
|---|---|
| 存量 3 万张 OCR 需数十分钟 | 分级入库：分钟级可浏览，OCR 后台低优先级补，逐张点亮 |
| 低配机器推理慢 | 一期仅 OCR（轻）；重模型全部二期且可关 |
| 批量移动文件引发用户恐慌 | 强制预览 + 撤销 + 回收站 + 操作日志 |
| pywebview 窗口体验一般 | 壳与内核解耦，保留换 Tauri 路线 |
| 云 API 隐私顾虑 | 默认全部本地；云端逐项开关，截图类内容默认不外传 |
