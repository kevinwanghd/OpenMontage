# Short-drama 5 分钟 Quickstart（零 Key、全离线）

本指南带你在 **5 分钟内**跑通 M0 短剧审核闭环：

```
om doctor  →  om demo --mock  →  om review list  →  om review decide  →  om review stats
```

全程 **零 API Key、零外部网络调用**。`om demo --mock` 只写本地文件，输出里会明确给出
`"external_api_calls": 0`。

> M0 只验证本地文件契约（审核队列、决策日志、统计）。真实图像/视频生成属于 M1，
> 没有 Key 时 `om doctor` 报告 `BLOCKED` 是**预期行为**，不影响本指南的 hello-world。

> **审核优先（M1）**：正式生成前先审剧本（`script`）和分镜脚本（`scene_plan`）。
> 两阶段的 checkpoint 必须为 `completed + human_approved`，`om demo --real`
> 才会跨过创作阶段进入付费生成；否则在任何外呼之前暂停并给出修复指引。
> 浏览器审核台：先 `om serve`，再打开 `http://127.0.0.1:8765/review-console/`。

---

## 前置条件

| 项 | 要求 |
|---|---|
| Python | 3.12（与本仓库开发环境一致） |
| 系统 | Windows 10/11 PowerShell（macOS/Linux 把 `.\.venv\Scripts\` 换成 `.venv/bin/` 即可） |
| 网络 | 不需要 |
| API Key | 不需要 |

> **音乐库前置条件（tiktok-draft，D20/AUD-10）**：`tiktok-draft` profile 的 music 槽只绑定本地
> `music_library/`，不走生成 API、也没有付费回退。进入真实生成（`om demo --real`）前，请在
> 项目根准备 `music_library/`，放入免版税曲目（可用 `MUSIC_LIBRARY_DIR` 指向其它目录），并在
> `music_library/licenses.json` 登记：`commercial_use` 必须为 `true` 才能进成片，
> `attribution_required: true` 的曲目需在成片署名。空曲库会让槽不可用，brief 门 preflight
> 直接 BLOCKED，在花视频预算之前停下。登记字段与机检规则见
> [shortdrama-music-library.md](shortdrama-music-library.md)。本指南前面的 mock hello-world
> 不生成画面、不需要曲库。

---

## 0. 安装（Windows PowerShell）

在仓库根目录执行：

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e .
om --help
```

`pip install -e .` 会按 `setup.py` 的 `console_scripts` 注册 `om` 命令。

如果 `om` 不在 PATH（例如没有激活虚拟环境、或 PowerShell 执行策略阻止激活），
可以直接用模块入口，效果完全等价：

```powershell
.\.venv\Scripts\python.exe -m shortdrama --help
```

**本文档后面所有 `om ...` 都可以替换为 `.\.venv\Scripts\python.exe -m shortdrama ...`。**

---

## 1. 体检：`om doctor`

```powershell
om doctor --json
```

输出结构（节选）：

```json
{
  "status": "BLOCKED",
  "profile": "tiktok-draft",
  "reasons": [
    {
      "code": "CREDENTIALS_MISSING",
      "slot": "tts_voice",
      "severity": "blocker",
      "message": "doubao_tts is unavailable (missing credential/dependency)"
    }
  ],
  "degradation": [ ... ],
  "cost_comparison": [ ... ]
}
```

三态含义：

| status | 含义 | 该做什么 |
|---|---|---|
| `GREEN` | 所有 required slot 有可用绑定 | 可以进入真实生成（M1） |
| `DEGRADED` | 主绑定不可用，但有预声明降级路径 | 看 `degradation[]` 的 `approval_required` 与成本 |
| `BLOCKED` | 有 required slot 无法满足 | 配置 Key，或先继续离线 hello-world |

**零 Key 时看到 `BLOCKED` 且退出码为 `4` 是正常的**，说明 doctor 正确识别了缺失凭据，
而不是安装失败。doctor 只读本地 registry/bindings，不发起网络请求。

```powershell
om doctor --profile tiktok-draft --json
```

---

## 2. 造一个 mock 剧集：`om demo --mock`

```powershell
om demo --mock --json
```

真实输出（路径随你的机器变化）：

```json
{
  "mock": true,
  "project_id": "demo",
  "episode_id": "ep01",
  "episode_dir": "...\\projects\\demo\\ep01",
  "created_manifest": true,
  "seeded_items": ["r_demo_hook", "r_demo_reaction"],
  "external_api_calls": 0,
  "next": [
    "om review list --episode ep01 --json",
    "om review decide r_demo_hook --action accept --yes",
    "om review stats --json"
  ]
}
```

它会：

1. 创建 `projects/demo/ep01/episode_manifest.json`（若已存在则不覆盖）；
2. 向 `projects/demo/ep01/reviews.jsonl` 追加两条待审项
   （`r_demo_hook`、`r_demo_reaction`）；
3. 输出 `external_api_calls: 0`。

> `om demo` 必须显式带 `--mock` 或 `--real`，两者缺失或同时给出都返回退出码 `2`。

### 2.1 真实驱动（`om demo --real`，需显式批准 + 预算）

`--real` 按 `pipeline_defs/short-drama.yaml` 的 9 个 stage 顺序驱动，工具经
`bindings/` 的 profile 槽位解析（改 profile 不改编排）。它默认拒绝执行，必须同时给出
**真人批准**和**正数预算**：

```powershell
om demo --real --approve-live --budget-usd 5 --json
```

- 任缺其一（或 `--budget-usd` 非正数）→ `VALIDATION_ERROR`，
  `recovery: do-X-first`、`side_effects: none`，不会写 ledger、不会调用任何 provider。
- 每次付费调用前先做原子预算检查（已花 + 已预留 + 本次最坏估算，含最多 3 次重试），
  再向 `SubmissionLedger` 写 submission intent；价格未知或超限会暂停并返回
  `BUDGET_EXCEEDED`（退出码 `5`），不会盲花。
- 提交后未拿到 task id 就失败 → ledger 记为 `unknown`，下次运行 `resume_plan` 返回
  `reconcile`，**禁止自动重复付费**，须先人工/查单对账。
- 声明的降级路径（如无原生口型 → TTS + lipsync + mux、480P → 1080P upscale）和
  provider 回退会在执行前写入 `projects/<project>/decision_log.jsonl`（NFR-06），
  不会静默替换。

真实调用会产生费用与外部 API 请求；测试与 CI 一律只用 mock/假执行器。

---

## 3. 看队列：`om review list`

```powershell
om review list --episode ep01
```

人类可读输出（真实形状）：

```
pending: 2
policy: {"mode": "sampled", "detail_ratio": 0.35, "face_sim_threshold": 0.75, "sampling_salt": "ep01"}
items: [...]
  r_demo_hook	shot_gen	sh_001	priority=0	candidates=2
  r_demo_reaction	shot_gen	sh_002	priority=0	candidates=1
```

机器可读：

```powershell
om review list --episode ep01 --json
```

退出码：

- `0`：队列里有待审项；
- `3`：`NOTHING_PENDING`，没有待审项（不是错误，可以去看 stats 或 `--stale`）。

筛选与“过期决策”：

```powershell
om review list --episode ep01 --stage shot_gen --priority 0
om review list --stale          # 决策落在已被替换的 candidate_version 上
```

---

## 4. 做决策：`om review decide`

### 4.1 通过

```powershell
om review decide r_demo_hook --action accept --yes --json
```

输出包含 `review_id`、`candidate_version`、`review_version` 与完整 `decision`
（`action`、`decided_at`、`decided_by` 等）。

### 4.2 驳回（必须给原因码）

```powershell
om review decide r_demo_reaction --action reject --reason MOTION_WRONG --yes
```

`shot_gen` 阶段允许的原因码见
[shortdrama-error-codes.md](shortdrama-error-codes.md#原因码与阶段适用范围)。
漏掉 `--reason` 会返回 `MISSING_REASON_CODE` 和退出码 `2`：

```json
{
  "code": "MISSING_REASON_CODE",
  "what": "reject was submitted without a reason_code",
  "fix": "retry with --reason CODE (API: reason_code)",
  "recovery": "fix-then-retry",
  "side_effects": "none",
  "context": { "review_id": "r_demo_reaction", "stage": "shot_gen" }
}
```

### 4.3 先校验、不落盘（`--dry-run`）

```powershell
om review decide r_demo_reaction --action accept --dry-run --json
```

`--dry-run` 会走真实的 `lib.review.ReviewStore.decide` 校验（同一套动作/原因码/候选/过期
规则），但**不写** `reviews.jsonl`。它可以用来预检 API/CLI 会怎么拒绝。

### 4.4 六个动作

`--action accept|reject|pick-variant|edit-prompt|send-back|skip`
（连字符或下划线都接受，例如 `pick_variant`）。

> `reject`、`send-back`、`edit-prompt` 属于有代价动作：交互式终端会询问确认；
> **非交互模式必须显式加 `--yes`**，否则返回退出码 `2`（绝不默认同意）。

---

## 5. 看统计：`om review stats`

```powershell
om review stats --json
```

真实输出（节选）：

```json
{
  "scope": "all",
  "episodes": ["ep01"],
  "review": {
    "total_items": 2,
    "decided": 1,
    "pending": 1,
    "action_distribution": { "accept": 1 },
    "reason_code_distribution": {},
    "first_pass_accept_rate": 1.0
  },
  "total_shots": 0,
  "shot_states": {}
}
```

只看某一集：

```powershell
om review stats --episode ep01 --json
```

---

## 6. 收尾：查看单项与整片 watch

```powershell
om review show r_demo_hook --json
om review watch ep01 --stage assembly --json
```

`om review watch` 在 demo 中没有成片媒体，会输出 `"state": "not-generated"` 并返回
**退出码 `3`**；这是“还没生成”，不是错误。

真实成片存在时，`--verify` 才会记录一条版本化的 `full_watch` 决策（见 runbook）。

---

## 7. 数据落在哪里

```
projects/
└── demo/
    └── ep01/
        ├── episode_manifest.json   # 剧集清单 + review_policy
        ├── reviews.jsonl           # 追加式审核日志（item/decision/undo）
        ├── review_policy.json      # 仅 review 层直接写策略时出现（可选）
        └── events.jsonl            # 镜头状态事件（watch/stats 读取）
```

`--projects-dir` 是**全局**参数，必须放在子命令前面：

```powershell
om --projects-dir .\my-projects review list --episode ep01 --json
```

默认根目录由 `OPENMONTAGE_PROJECTS_DIR` 决定，未设置时是仓库根的 `projects/`。

### 双 agent 模型配置（v2.5 DUAL-02/09）

`bindings/agent_models.yaml` 声明 5 个生产角色（`script` / `visual` / `shot` /
`edit_sound` / `director`）各自的 producer 与 critic 模型：

- **硬约束（D19）**：同一角色的 producer 与 critic 必须**不同模型家族**；
  违例时 `lib.agent_models.ensure_valid()` 拒绝启动（BLOCKED），
  不会跑一个假的对抗审核。
- **P0 默认**：producer=`deepseek-v4.1-flash-1017`（family `deepseek`）、
  critic=`glm-5.3`（family `zhipu`）。换模型只改这个文件，不需要动代码；
  家族字段是校验依据，不是装饰。
- 环境变量 `AGENT_MODELS_PATH` 可指向另一份配置（多项目复用/测试）。
- 自检命令：

```powershell
python -c "from lib.agent_models import ensure_valid; ensure_valid(); print('agent models ok')"
```

配置改动后建议先跑这条，再进入生产阶段——同家族配置会在启动门直接 BLOCKED。

---

## 8. 清理

```powershell
Remove-Item -Recurse -Force .\projects\demo
```

（只想重来一遍也可以先删掉这个目录，再执行 `om demo --mock`。）

---

## 常见问题

| 现象 | 原因 | 处理 |
|---|---|---|
| `om` 不是可识别的命令 | 未激活 venv 或未 `pip install -e .` | 用 `.\.venv\Scripts\python.exe -m shortdrama ...` |
| `doctor` 返回 `BLOCKED` / 退出码 4 | 零 Key，required slot 缺凭据 | 预期行为；配置 Key 后再查，或继续离线流程 |
| `demo --real` 在 brief 门停下，报 `SlotUnavailable` / `MUSIC_UNAVAILABLE` | `tiktok-draft` 只认本地曲库，空库不可用 | 按 [shortdrama-music-library.md](shortdrama-music-library.md) 把曲目放进 `music_library/` 并在 `licenses.json` 登记 |
| `review list` 退出码 3 | 没有待审项 | 正常；用 `--stale` 看待补偿项或跑 `demo --mock` |
| `decide` 退出码 2，提示需要确认 | 非交互模式未加 `--yes` | 加 `--yes`（仅在你确认动作有代价时） |
| `decide` 报 `MISSING_REASON_CODE` | reject 没带原因码 | 加 `--reason <CODE>` |
| `watch` 退出码 3 / `not-generated` | 还没有成片媒体 | 先完成组装，再 `--verify` |
| PowerShell 中文/符号乱码 | 控制台编码与 GBK 不兼容 | 用 `--json`（输出稳定），CLI 自身已做编码降级 |

---

下一步：

- 完整命令文法与退出码见 [shortdrama-cli-reference.md](shortdrama-cli-reference.md)。
- 错误信封字段与全部错误码见 [shortdrama-error-codes.md](shortdrama-error-codes.md)。
- L1/L2/L3、抽检、batch、undo、stale、整片 watch 见
  [shortdrama-review-runbook.md](shortdrama-review-runbook.md)。