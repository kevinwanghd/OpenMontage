---
name: holopix-video
description: Generate video with a Holopix (holopix.cn) membership through its web API. Covers the encrypted websocket transport, the Seedance parameter object, and the compute-point cost model.
---

# Holopix video generation

The `holopix_video` tool wraps this API. Holopix has no public API. The web app
talks to `https://holopix.cn/pino_service_api` with a session token, and
every frame it sends is base64(AES-128-CBC) with a static key that ships in
the app bundle. `tools/_holopix/client.py` implements that contract; call
it, do not hand-roll requests.

Video generation runs over a **websocket**
(`wss://holopix.cn/pino_service_api/websocket/<apiClientId>`), not over the
legacy HTTP submit - see the failure table below.

## Credential

```
HOLOPIX_TOKEN=...   # session token; see tools/_holopix/client.py
```

Get one with `python -m tools._holopix.client login --phone <phone>` then
`--code <sms code>`, or `python -m tools._holopix.client harvest` against a
Chrome started with `--remote-debugging-port`.

## Request envelope

- POST bodies are encrypted: `base64(AES-128-CBC(JSON({"data": {...}})))`.
- Responses are encrypted the same way; plain JSON means the request never
  reached the payload parser (for example "解密失败").
- GET endpoints take a plain body.
- The video endpoint nests one more level: `{"data": {"message": "<json string>"}}`.
  `message` is a **string** holding the parameter object as JSON - the server
  parses it a second time. Passing an object there fails with
  `Cannot deserialize instance of java.lang.String`.

## The video parameter object

`seedance_param()` builds it, field for field like the web app's own
`mJe()` builder:

| Field | Meaning |
|-------|---------|
| `module` | the id of the (model, mode) pair - see the table below |
| `userId`, `uniqueId` | `user_info()` |
| `artworkId` | `create_artwork()` |
| `apiClientId` | `<user id>-<epoch millis><random digits>`, the shape the app generates; also the task key and the websocket path |
| `source`, `startTime`, `ctime` | `1` and the epoch milliseconds of the run |
| `vipChannel` | `true` only on the 加速 (expedite) channel; an account without speed credits sends `false` |
| `videoModel` | `seedance-2-0`, `seedance-mini`, `minimax-h3`, `minimax-h3-fast`, `minimax-h3-max` |
| `videoMode` | `text-to-video`, `image-to-video`, `start-end`, `reference` |
| `prompt` | the prompt text |
| `videoDuration` | 4-15 seconds |
| `videoRatio`, `calcRatio` | `16:9`, `9:16`, `1:1`, `4:3`, `3:4`, `21:9` |
| `videoResolution` | `480p`, `720p`, `1080p` |
| `imageList` | reference images (public URLs) |

The `module` id is decided by the model and the mode together:

| model | reference | start-end | image-to-video | text-to-video |
|-------|-----------|-----------|----------------|---------------|
| `seedance-2-0` | 76 | 75 | 77 | **78** |
| `seedance-mini` | 80 | 79 | 81 | 82 |

`mode`, `modelKey`, `quality` and `duration` are **not** server fields: the
server ignores them. Do **not** send `promptImgId` as an empty string: the
server parses it as a number and answers `NumberFormatException: empty
String`.

## Two steps per run

1. `create_artwork()` - POST `/api/artwork/add` with
   `{artworkName, modelList: "[]", lastModule}`; the response data is the
   artwork id. The server allows only **one running video task per account**,
   so a second submission while one is in flight answers
   `已存在生成任务，请稍后重试`.
2. `submit_video_ws()` - open
   `wss://holopix.cn/pino_service_api/websocket/<apiClientId>` and send the
   **encrypted** parameter object.

The websocket is the live path. Its handshake carries the session in a
cookie named `token_production` (not `token_prod`), and the frames it sends
back are encrypted with the same AES key: `conn_success`, then task frames
with `status` 1 (queued), 2 (generating), 3 (done, carries `videoUrl`),
-1 (failed), -2/-3 (other errors), interleaved with `heartBeat`.

The finished clip also shows up in the artwork's version list
(`/api/artwork/versionList`) and in `/api/artwork/moduleVersion`, as a
version carrying a `videoUrl`.

## Prompting

- The upstream models are Seedance-backed, so Chinese prompts work well and
  respond to explicit camera language ("镜头缓慢推进", "航拍下降").
- Keep the prompt under the model's character limit and put the motion first:
  what the camera does, then the subject, then the light.
- One run is one shot. Ask for a single continuous move; a prompt that
  describes several cuts produces a mush.

## Cost

Every generation spends the account's compute points (算力). `rights()` shows
the balance and `points_record()` shows what each run cost. When the balance
is too low the server refuses with `灵力值不足`; there is no partial run and
no automatic retry that helps.

## Failure modes

| Message | Meaning |
|---------|---------|
| `解密失败` | body was not encrypted, or the app changed its key |
| `登录已过期` / `token` errors | session expired; re-run `login` or `harvest` |
| `服务繁忙` | the model is busy; pick another model or retry later |
| `灵力值不足` | not enough compute points |
| `视频生成失败，算力已返还` | the upstream model refused the run; the credits come back - see below |
| `已存在生成任务，请稍后重试` | one video task per account at a time - wait |
| `作品不存在` | the artwork id is stale - create a new one |
| `apiClientId不存在` | the parameter object is missing `apiClientId` |

## Cost

One 480p/5s run costs about 190 credits (算力); 720p and 1080p cost more.
`rights()` returns `totalPoints` (spendable) and `foreverPoints` (permanent
credits). A failed run is refunded and shows up as a positive ledger row.

## `视频生成失败，算力已返还` means you used the dead HTTP path

The legacy REST endpoint `POST /aiApi/api/generateVolceVideo` refuses
**every** run with this message, whatever the payload: the server charges
190 credits, finds no task to create, and refunds (the ledger shows a `sub`
of 190 followed by an `add` of 190 with `"compensation": true`). It is not a
malformed request and not an upstream outage - it is the wrong transport.
The same account produces videos without trouble over the websocket.

The tell: the request fails **synchronously**, with `code: -1`, and no task
row is ever created, so there is nothing to poll. A real run queues
(`status: 1`) and finishes with `status: 3` and a `videoUrl`.

The client raises `HolopixUpstreamError` for this case, so a caller can tell
it apart from a busy account (`HolopixBusyError`, worth waiting on) and from
a bad request (`HolopixError`). The tool reports it as refunded and not
resumable, because there is no task to collect.

Use `submit_video_ws()`, or just call the `holopix_video` tool, which uses
the websocket.
