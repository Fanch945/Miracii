# Miracii
A Self-made Chat Robot with Custom Persona and Upgrading Memory Construction

本地人格伴侣：**v0.0.1（测试）**。说话模型负责对话；监督模型负责巩固时钟、印象和知识候审。窗口是 RAM，文件是磁盘。

## 怎么跑

桌面窗口（含网页服务 + 心跳）：

```text
pip install -r requirements.txt
python -m app
```

或双击仓库根目录的 `Miracii.bat`。

只开浏览器、本机访问：

```text
python -m app --web
```

给手机连（电脑与手机同一局域网）：

```text
python -m app --lan --web
```

或双击 `Miracii-lan.bat`，把打印出来的 `http://<局域网IP>:7788` 填进 Android 客户端。

密钥：`APIkeys.txt`，一行一个 `provider=alias=secret`。说话用 `dsapi=Zero=sk-...`，监督用 `sumapi=...=sk-...`。说话模型是 **deepseek-flash**（V4.1，可看图）。输入栏可点「图」、粘贴或拖入 JPEG/PNG/GIF/WebP。

## 打 Android APK

不要用 Visual Studio。打开 `packaging/android/` 要用 **Android Studio**（自带 SDK）。步骤见该目录 README：同步 Gradle → `assembleDebug` → `app/build/outputs/apk/debug/app-debug.apk`。电脑先 `python -m app --lan --web`。

## 架构（四层 + 两个进程）

1. **身份** — `persona/<id>/profile.txt` 等，每轮都在。
2. **设定 lore** — 作者预写的百科，不是记忆。
3. **记忆 memory** — 你们后来才发生的事：`working` → `impression` → 日周 `learn_queue`。
4. **原文 transcripts** — `data/transcripts/`，不删，记忆坏了可以重摘要。

进程：

- `python -m app`：可视化窗口 + HTTP（`app.main`）+ 心跳（`app.heartbeat`）
- 网页只负责聊。心跳按 5 分钟 STM / 30 分钟印象 / 日周叫醒监督。

细节：`docs/HOW_PERSONA_RUNS.txt`、`docs/CONSOLIDATION.txt`、`docs/HEARTBEAT.txt`。

## 监督 API 的输入、输出、日志

心跳每次 tick：

1. 组 packet（`app/supervisor.py` `build_packet`）：时刻、phase、quiet、working、**近段原文 `transcript_tail`**、**已有印象 `impression_tail`**、日程、keeper 尾。
2. 当 system 的是 `skills/supervisor/SKILL.md`；当 user 的是 packet 的 JSON。调 `sumapi`（DeepSeek）。
3. 模型必须只回一个 JSON：`speak` / `impression` / `working_note` / `learn` / `tasks` 等。
4. 运行时执行：routine 把 `impression` 写入按日文件；`learn` 追加到 `pending.jsonl`。API 失败则本地 gist，不写空占位。

落盘（控制台 → 日志）：

| 文件 | 内容 |
|---|---|
| `data/supervisor.jsonl` | **完整** packet 入、decision 出，一条 tick 一行。`source` 为 `sumapi` / `rule` / `sumapi-fail` |
| `data/heartbeat.log` | 一行摘要：phase、source、是否写出印象、learn 条数 |
| `data/keeper.log` | 对话原文保存 |
| `data/clock.json` | 巩固时钟状态 |

v0.0.1 之前**没有** `supervisor.jsonl`。旧的 `heartbeat.log` 只记了标志位（`to_impression: true`），**没有**模型原文，也**没有**把监督写的段落落到印象区。当时 routine 实际走的是代码里的 `_blur`：把对白截到 48 字加省略号。`learn_queue/pending.jsonl` 里 `source: clock-daily` 的「尚未写入设定」是时钟占位，不是监督抽出来的事实。

诊断时先看 `supervisor.jsonl` 的 `source` 和 `decision.impression` / `decision.learn`，再看 `applied`。

## 印象怎么写

人过了大约半小时，记得的是场面和感觉，不是原话。一场坐下来追加一块到 `persona/<id>/memory/impression/YYYY-MM-DD.txt`：

```text
# routine 2026-09-12 14:47
那次：午饭后靠在一起。他让两点叫醒放松一下；她答应了，后来自己睡着，晚了十来分钟，有点心虚。还提过以后很难同实验室。
气氛：困、亲近、带点抱歉
keys: 计时, 午睡, 实验室
```

- 用「那次」，大约时段（午饭前后 / 夜里），不要钟表抄录。
- 禁止 `你：…；她：…` 和把句子切开放省略号。
- 监督应把这段放在 JSON 的 `impression` 字段。若它漏写或又贴了截断对白，运行时会改成本地 gist（话题 + 气氛），仍然不是对白切片。

模版样例：`persona/_template/memory/impression/sample.txt`。监督口吻：`skills/supervisor/SKILL.md`。

STM（5 分钟）仍可以较具体，那是复述，还不是印象。

主动开口、工具、要不要换 GPT：见 `docs/AGENT.txt`。测试无输入开口：`python -m app.heartbeat --once idle --force-speak`（页面开着才会看见）。

## 手机 Android（v0.0.1）

仓库里是一个 WebView 客户端：连电脑上的 Miracii，不在手机里跑模型。

1. 电脑：`python -m app --lan --web`
2. Android Studio 打开 `packaging/android/`
3. 装到手机，设置里填 `http://<电脑局域网IP>:7788`

没有 Android SDK 时，用系统浏览器打开同一个地址也可以；页面已按窄屏排过。

## 版本

测试版 **0.0.1**。会改协议、文件格式和封装方式。不要当稳定发行。
