---
name: molica-video
description: Generate video through a Molica membership (web.molica.com.cn) — Veo 3.1, Sora 2, Seedance 2/2.5, Jimeng, Wan 3.0, MiniMax H3, Luma, zhipu and more behind one prepaid account. Use when the user has a Molica membership and wants its models instead of a metered vendor API key.
---

# Molica video (membership-backed)

`molica_video` talks to Molica's own web API with the account's session token.
Molica publishes no API key and no public docs: the tool speaks the same
contract the web client uses. Every generation spends the account's prepaid
credits (灵力) — there is no separate cash charge per video.

Route it through `video_selector` (`preferred_tool="molica_video"`), or call
`molica_video` directly when the user names Molica or an exact Molica model.

## Auth

`MOLICA_TOKEN` in `.env` holds the session token. Refresh it with:

```bash
python -m tools._molica.client harvest   # take the token from a logged-in Chrome
python -m tools._molica.client check     # verify the stored token
python -m tools._molica.client login --phone <n> --code <sms>
```

`harvest` reads the token the web app already stores in Chrome's local
storage, so the fastest path is: log in at
https://web.molica.com.cn/chat/new (WeChat scan), then `harvest`.

## Preflight every paid call

1. `tool.get_status()` must be `available` — otherwise the token is missing or
   expired, and no request will be accepted.
2. Read the live catalog before choosing a model. `POST /api/v1/video/params`
   needs no auth and lists every model with its durations, resolutions and
   aspect ratios:

   ```python
   from tools._molica.client import MolicaClient
   for m in MolicaClient().video_params():
       print(m["model_key"], m.get("create_mode", {}).get("text_to_video", {}).get("enable"))
   ```

   `get_info()["model_catalog"]` carries a snapshot of the same keys for
   routing; the live endpoint is authoritative.
3. Check the remaining balance: `POST /api/v1/integral/balance/query` returns
   `integral_balance`. A 5-second clip costs on the order of 105–171 灵力, and
   a generation that cannot be paid for is rejected by the server — the tool
   refuses before creating a task when the balance is under the cheapest known
   run. This credit balance — not a USD estimate — is the real budget here.
4. Announce the model, duration, resolution and the expected credit spend
   before submitting. The credit price is unknown to the tool until the
   account's own ledger shows it, so `estimate_cost` asks for a quote rather
   than reporting a misleading `0.0`: set `MOLICA_COST_USD_PER_VIDEO` for a
   flat price, or `MOLICA_USD_PER_CREDIT` to convert credits into USD. Without
   one of those the selector treats this provider as unpriced, not as free.

## Model availability is not stable

A model whose upstream compute is congested answers `ret=500` ("service
busy") and **no task is created**. This is per-model and transient: at the
same moment one model may refuse while another serves.

The tool fails over on its own: it tries the requested model first, then up to
two more (known-serving models first, then anything else the account can
reach), stops as soon as one serves, and reports `parameters_adapted` when a
substitute model could not take the requested duration or resolution. Pass
`disable_failover: true` to fail instead of switching.

Do not retry the same busy model in a loop — the upstream is shared, and the
next attempt will be refused the same way.

Models observed serving normally: `molicas2`, `wan30video`, `wan30prime`,
`wanx26`, `wanx27`. Models observed congested: `veo31pro`, `veo31`,
`jimeng3pro`, `jimeng35pro`, `seedance2`, `sora2`, `luma`, `minimaxh3`.

## Request shape

```python
tool.execute({
    "prompt": "...",
    "model": "molicas2",
    "duration": 5,
    "aspect_ratio": "16:9",
    "resolution": "720p",
    "fast_model": True,       # fast queue; off is slower but cheaper
    "expand_prompt": False,   # let Molica rewrite the prompt
    "output_path": "projects/demo/clip.mp4",
})
```

- `duration`, `resolution` and `aspect_ratio` are validated against the live
  catalog before the task is created, so an impossible combination fails
  before any credit is spent.
- Reference images accept **public URLs** (`image_url`, `image_end_url`,
  `reference_image_urls`) and **local files** (`image_path`, `image_end_path`,
  `reference_image_paths`). Local files are uploaded to Molica's own object
  storage, which needs the `oss2` package; without it the tool falls back to
  the repository's existing uploader (`upload_image_fal`, needs `FAL_KEY`) and
  still hands Molica a public URL.
- `fast_model` uses the priority queue. Turn it off for cheaper standard
  generation when the timeline allows.
- The create endpoint does not always echo a task id. The tool resolves it by
  matching the prompt and model in the account's task list, then polls until
  the task finishes, downloads the mp4 and probes it. `timeout_seconds`
  (default 1200) and `poll_interval_seconds` (default 8) control the wait.

## Running it inside a pipeline

`scripts/molica_cinematic_run.py` drives a full `cinematic` production whose
scene clips come from this provider — real artifacts, checkpoints and Backlot
board events.

```bash
python scripts/molica_cinematic_run.py --preflight --scenes 2 --model wan30video
python scripts/molica_cinematic_run.py --scenes 2 --model wan30video
python scripts/molica_cinematic_run.py --scenes 2 --wait-approval   # stop at every gate
python scripts/molica_cinematic_run.py --scenes 2 --resume           # reuse existing assets
```

`--preflight` prints the credit plan (price per run, account balance, whether
it adds up) and exits without spending anything — run it before a batch when
the account balance is tight.

The run produces the whole pipeline: research, proposal, script, scene plan,
assets (Molica clips, a TTS narration per scene, a burned-in SRT and a music
bed mixed with ducking), edit decisions, a rendered mp4, and a publish log —
with `decision_log` and `final_review` artifacts alongside the checkpointed
stages.

The board runs at `http://127.0.0.1:4750/p/<project-id>` (`python -m backlot serve`).
Use `--render-runtime ffmpeg` for a cut-only piece; Remotion needs Chromium
headroom that a busy workstation may not have.

## Task states

`waiting` means queued, `status=0` means generating, `status=4` means failed
(the server's `failure_reason` says why, usually "AI算力抖动" — upstream
compute jitter). A finished task carries `video_url` and `thumbnail_url`.
