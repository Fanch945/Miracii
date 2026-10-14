# AGENTS.md — Miracii

给在这个仓库里干活的所有 agent（DSH、Cursor、以及以后接手的）看。**先读这一页再动手。**

## 这个项目是什么

本地单人用的 AI 伙伴：一个长期人格 + 分层记忆 + 巩固链 + 桌面提醒。
FastAPI 后端 + pywebview 窗口，数据全在本机，不依赖云。

## 先看哪里

| 想了解 | 看 |
|---|---|
| 积温（主动开口）怎么工作 | [docs/WARMTH.txt](docs/WARMTH.txt)：六轴算法、维度影响、观察方式、**标注与校准** |
| 记忆元数据层（星图） | [docs/STARMAP.txt](docs/STARMAP.txt) |
| 桌面提醒 | [docs/NOTIFY.txt](docs/NOTIFY.txt) |
| 外部资料要点（已核实/未核实） | [docs/reference/](docs/reference/) |
| **想做但没做的事** | [docs/BACKLOG.txt](docs/BACKLOG.txt)：为什么 / 多大 / 卡在哪 |
| 某一次改了什么 | [docs/changes/](docs/changes/)：一条改动一份，含复核与回应 |

## 常用命令

```powershell
.venv\Scripts\python.exe -m app                       # 起应用（含心跳；默认 http://127.0.0.1:7788）
.venv\Scripts\python.exe -m unittest discover -s tests -t .   # 全量测试（提交前必跑）
.venv\Scripts\python.exe -m app.warmth --watch        # 实时看她的六轴
.venv\Scripts\python.exe -m app.warmth --stats        # 标注汇总（校准前先看这个）
powershell -File tools\review.ps1 -Record docs\changes\<记录>.txt -Ask "<重点看什么>"   # 起一次复核
```

界面入口：应用顶栏「积温」→ `/warmth` 面板；底部命令框支持 `ls` / `open` / `runtime` / `warmth` / `warmth watch` / `mark`。

## 红线

1. **不改 `persona/<id>/` 下记忆与设定的实际内容**，保留原有的分类文件树。
   程序自己生成的派生文件（`memory/starmap.jsonl`、`memory/warmth*.json`）可以删、可以重建，**不要手改**。
2. **界面里不做真终端**（PTY / 任意 shell）。应用能 `--lan` 绑 0.0.0.0 给手机用而接口无鉴权；
   命令窗口是**白名单分发器**，这个结构本身就是安全边界。要加能力就再加白名单命令。
3. **用户输入不可信**：台账里有用户写的标注和她自己的原话。前端**不要用 `innerHTML` 拼这些文字**——
   一次实测里 `<img onerror>` 是能执行的（`docs/changes/2026-10-14-warmth-panel.txt`）。

## 报告格式

1. **全面**：背景、现状、改动点、证据、风险与未决；验证过的与没验证的分开写，**不省略失败项**。
2. **分点**：`##` 分节，结论用短句，一点一事；不写大段散文。
3. **逻辑清晰**：按「结论 → 做法 → 证据 → 影响 → 下一步」推进，先判断后依据。
4. **结合样例**：贴实际命令与关键输出（长输出截断并标明）；**不臆造没跑过的结果**。
5. **收尾**：做了什么、影响什么、还剩什么。

## 改动登记

在 `docs/changes/YYYY-MM-DD-<短名>.txt` 留账，模板见 [_TEMPLATE.txt](docs/changes/_TEMPLATE.txt)、规矩见 [README.txt](docs/changes/README.txt)。

- 判据：**改完之后，别人要重跑一次或重读代码才知道行为变了 → 登记。** 拿不准就写。
- **角色不要混**：作者写「背景 / 改了什么 / 验证 / 风险与未决」；复核方只追加「复核（谁 / 日期）」；
  **只有作者**写「回应：」逐条答复（采纳 / 不采纳 + 理由）。复核方**不修改对方写过的字**，分歧保留。
- 同一天、同一主题的后续改动追加到同一条记录的「后续」小节，不另开文件。
- **验证**只写实际跑过的命令与输出；没跑过写「未验证」。
- 规矩自身的改动也按普通记录登记。

## 复核是默认步骤，不是可选项

**只要动了代码或行为，就走一遍。** 完整交付五步：

| 步 | 谁 | 做法 |
|---|---|---|
| 1 写 | 作者 | 记录前四节，复核节留空 |
| 2 复核 | **另一方** | `powershell -File tools\review.ps1 -Record docs\changes\<记录>.txt -Ask "<重点看什么>"` |
| 3 回应 | 只有作者 | 逐条「回应：」——采纳 / 不采纳 + 理由 |
| 4 复跑 | 复核方 | 按记录里的命令再跑一遍，写「复跑（谁 / 日期）」 |
| 5 入库 | 作者 | 提交推送；对方的原文一并入库 |

- **复核方必须自己取证**：跑测试、跑复现命令、用临时 DOM / 临时目录观察行为。只读代码提意见不算复核。
- **意见要能执行**：附文件 + 行号 + 一条可复跑的验证命令。没真问题就写"通过"。
- **复核期间，作者不要动代码**：`review.ps1` 会比对 `app/`、`tests/` 的 SHA256 并报警——
  上一次告警就是作者自己在并行提交。要么等，要么事先在记录里声明。
- 跳过复核要写明理由（纯文档措辞、错别字、格式化可以跳）。
- **为什么值得**：2026-10-14 一天四轮复核抓出 **24 条**真问题，其中 6 条是"记录里声称做了、代码里没做"，
  2 条是安全/可用性级别。这类问题自查几乎发现不了。

## 跨 agent 协作

- **结论走文件，不走对话。** 两个 agent 之间要传的一律落进 `docs/changes/`；聊天记录不算交付物。
- **CLI 对话与 IDE 对话是两套独立存储，互不可见**（CLI 在 `~/.cursor/chats/`，IDE 在 Cursor 自己的库）。
- **调 Cursor Agent CLI 必须显式 `--workspace D:\SeelAI\Miracii`**，否则会话会归到别的项目名下。
- **参考仓库在仓库外**：`D:\SeelAI\ClaraShafiq\{MemoryConstellations,jiwen,sts2-companion-bridge}`（MIT）。

## 本机环境的坑（都踩过）

| 坑 | 表现 | 办法 |
|---|---|---|
| GBK 控制台 | 中文输出乱码；**一个 `⟵` 就让 `--review` 崩** | 给人看的输出只用 GBK 能编的字符；有测试守着（`encode("gbk")`） |
| UTF-8 BOM | PowerShell 5.1 按 GBK 读无 BOM 的 `.ps1` → 语法错误；JSON 配置读不了 | `.ps1` 存**带 BOM**；配置按 `utf-8-sig` 读 |
| 提交信息带 BOM | `git commit -F` 出来的标题前面多个字符 | 用 `UTF8Encoding($false)` 写临时文件 |
| Cursor CLI 挂住 | 20 分钟无响应 | `~/.cursor/cli-config.json` 的 `network.useHttp1ForAgent: true` |
| 端口 | 不是 8000，是 **7788**（`app/launch.py` 的 `DEFAULT_PORT`） | 换端口用 `--port` |
