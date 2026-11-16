# 短剧音乐库（D20 / AUD-10）

短剧 P0 的背景音乐**只从本地 `music_library/` 取**，不走生成 API、不抓网页音源。
这条规则由 `lib/music_licenses.py` 机检，装配前必须通过。

## 目录与登记

```
music_library/
├── licenses.json      # 曲目登记表（必需）
├── warm-piano-01.mp3
└── ...
```

`licenses.json` 的 `tracks[]` 每条必须包含：

| 字段 | 说明 |
|---|---|
| `file` | 相对 `music_library/` 的文件名（也是 `edit_decisions` 引用的 asset_id） |
| `title` / `artist` | 曲名与作者（署名检查用） |
| `source` | 来源（pixabay / archive.org / jamendo / own …） |
| `license` | 许可名称（如 `CC0 1.0`、`Pixabay Content License`） |
| `commercial_use` | 是否允许商用；**必须为 `true` 才能进成片** |
| `attribution_required` | 是否要求署名；`true` 时 delivery 权利文本必须包含 `artist - title` |
| `url` | 来源链接，便于复核 |
| `downloaded` | 下载日期 `YYYY-MM-DD` |
| `mood`（可选） | 情绪标签（piano / ambient / upbeat …） |

## 机检行为

- 曲库文件缺失：`MusicLicenseError`（硬错误，不静默放行）。
- `edit_decisions` 引用的曲目未登记：阻塞，提示「not registered」。
- `commercial_use != true`：阻塞。
- `attribution_required=true` 但 `artist`/`title` 为空：阻塞（空署名不能算通过）。
- `attribution_required=true` 且权利文本缺少 `artist - title`：阻塞。
- 没有任何音乐引用：零违规（但短剧 P0 的 sound_design 仍要求音乐床，见 AUD-08 修订）。

```powershell
python -c "from lib import music_licenses as m; print(m.audit({'audio': {'music': {'asset_id': 'warm-piano-01.mp3'}}}, m.load(), ''))"
```

## 空曲库时

`tiktok-draft` profile 的 music 槽只绑定 `music_library`，空曲库 → 槽不可用 →
brief 门 preflight 直接 BLOCKED（在花视频预算之前停）。补曲并登记后再继续。