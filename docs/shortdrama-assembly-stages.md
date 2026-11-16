# 短剧装配阶段拆分（v2.5 AUD-10 / VIS-11）

`pipeline_defs/short-drama.yaml` 的 assembly 阶段声明三个子阶段（`sub_stages`，
MOD-06/F14：声明式分支，不是代码 if）：

| 子阶段 | 条件 | 做什么 | 槽位 |
|---|---|---|---|
| `rough_cut` | `assembly_rough_cut` | 按 beat map 把已批准镜头剪成粗剪；导演审节奏、死气、钩子/re-hook、音乐点 | `compose` |
| `sound_design` | `assembly_sound_design` | 音乐情绪曲线（铺垫→swell→收），**只从 `music_library/` 选曲**；缺曲 `MUSIC_UNAVAILABLE` 即停；对白优先混音、响度达标 | `music`、`tts_voice` |
| `grade_finish` | `assembly_grade_finish` | 调色统一（掩盖跨镜色温/材质差）+ 字幕视觉（关键词强调/动态） | `compose`、`subtitle` |

- `sub_stages` 是既有 manifest 契约（shot_gen、delivery 早已使用），本次是声明增量，
  无 schema 变更、无需迁移；引擎按名字读取子阶段分支。
- **delivery 仍是独立顶层阶段**（第 9 阶段），不并入 assembly。
- 每个子阶段都带 `condition` / `description` / `review_focus`，由
  `tests/contracts/test_shortdrama_skills.py` 的
  `test_all_sub_stages_are_fully_predeclared` 校验。

音乐来源与许可要求见 [shortdrama-music-library.md](shortdrama-music-library.md)（D20/AUD-10）。