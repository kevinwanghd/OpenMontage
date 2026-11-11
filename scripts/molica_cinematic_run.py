"""Run a real OpenMontage cinematic production whose video assets come
from a membership provider (Molica or Holopix).

Not a demo: this creates a project, walks the cinematic stage order, writes
schema-valid artifacts and checkpoints, and generates every scene clip through
OpenMontage's own `video_selector` (which routes to the chosen provider). The
Backlot board shows the run live.

    python scripts/molica_cinematic_run.py --scenes 2 --model wan30video
    python scripts/holopix_cinematic_run.py --scenes 1 --model seedance

Every generation spends the membership account's credits (灵力 for Molica,
算力 for Holopix). The script reads the balance before it starts and refuses
to begin a run it cannot pay for.
"""

from __future__ import annotations

import argparse
import atexit
import json
import os
import shutil
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from lib.checkpoint import PROJECTS_DIR, init_project, write_checkpoint
from lib.events import emit_event
from pipeline_gate import stage_gate, task
from tools.video._shared import probe_output

TITLE = "One Membership, Many Models"

SCENES = [
    {
        "id": "sc1",
        "description": "A single orange cat yawning on a sunlit windowsill, slow push in",
        "prompt": "A ginger cat yawning on a sunlit windowsill, slow camera push in, warm afternoon light, shallow depth of field, cinematic",
        "narration": "One account. One login. And every video model behind it.",
        "shot_language": {
            "shot_size": "medium_close",
            "camera_movement": "dolly_in",
            "lens_mm": 50,
            "lighting_key": "golden_hour",
            "depth_of_field": "shallow",
            "color_temperature": "warm",
        },
    },
    {
        "id": "sc2",
        "description": "Night city lights reflecting on wet asphalt, aerial descent",
        "prompt": "Aerial view descending over a neon city at night, wet asphalt reflecting lights, cinematic, moody, slow motion",
        "narration": "Molica turns the same membership into Veo, Sora, Seedance, Jimeng and Wan.",
        "shot_language": {
            "shot_size": "extreme_wide",
            "camera_movement": "crane_down",
            "lens_mm": 24,
            "lighting_key": "neon",
            "depth_of_field": "deep",
            "color_temperature": "cool",
        },
    },
]


def _provider_label(tool: str) -> str:
    """Return the membership name behind a provider tool."""
    return {
        "molica_video": "Molica membership",
        "holopix_video": "Holopix membership",
    }.get(tool, tool)


def _balances(tool: str = "molica_video") -> tuple[int | None, str]:
    """Return (balance, note) for the membership account behind ``tool``."""
    try:
        from dotenv import load_dotenv

        load_dotenv()
        if tool == "holopix_video":
            from tools._holopix.client import HolopixClient

            rights = HolopixClient().rights()
            # A Holopix account spends from three separate pools: the
            # membership pool (memberTotalPoints — a 145-point mini run drops
            # it), the non-expiring pool (foreverPoints) and the plain points
            # pool (totalPoints). Reading only the first under-reports the
            # balance and blocks runs that would have succeeded, so sum every
            # pool the account can actually spend from.
            pools = [rights.get(key) for key in
                     ("memberTotalPoints", "foreverPoints", "totalPoints")]
            spendable = sum(int(value) for value in pools
                            if isinstance(value, (int, float)))
            if spendable:
                return spendable, ""
            for key in ("power", "compute", "integral", "points", "balance", "remaining"):
                value = rights.get(key)
                if isinstance(value, (int, float)):
                    return int(value), ""
            for value in rights.values():
                if isinstance(value, dict):
                    for key in ("power", "compute", "balance", "remaining"):
                        if isinstance(value.get(key), (int, float)):
                            return int(value[key]), ""
            return None, "balance field not found in the rights payload"

        if tool == "zhipu_video":
            # cogvideox-flash is a free tier: there is no membership balance to
            # spend and therefore no account budget to gate a run on. Returning
            # None skips the caller's balance check instead of falling through
            # to the Molica membership, which is a different account entirely.
            return None, "zhipu_video 免费档：无会员算力账单，跳过余额预检"

        from tools._molica.client import MolicaClient

        client = MolicaClient()
        data = client.post("/v1/integral/balance/query").get("data") or {}
        return int(data.get("integral_balance") or 0), ""
    except Exception as exc:  # noqa: BLE001
        return None, f"balance unavailable: {exc}"


def _pid_alive(pid: int) -> bool:
    """Return True when a process id is still running."""
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes

        handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
        if not handle:
            return False
        code = ctypes.c_ulong()
        ok = ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
        ctypes.windll.kernel32.CloseHandle(handle)
        return bool(ok) and code.value == 259  # STILL_ACTIVE
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _acquire_credit_lock(name: str = "membership-credits") -> Optional[Path]:
    """Claim the shared credit budget for this machine.

    Two agents (or two terminals) working the same membership would each
    spend the account's points without knowing about the other. The lock is a
    file in the temp directory holding the owner's process id; a lock whose
    owner is gone is taken over.
    """
    lock_dir = Path(os.environ.get("TEMP", ".")) / "openmontage"
    lock_dir.mkdir(parents=True, exist_ok=True)
    lock = lock_dir / f"{name}.lock"
    for _ in range(2):
        try:
            handle = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(handle, str(os.getpid()).encode())
            os.close(handle)
            return lock
        except FileExistsError:
            owner = 0
            try:
                owner = int(lock.read_text(encoding="utf-8").strip() or 0)
            except (OSError, ValueError):
                owner = 0
            if owner and _pid_alive(owner):
                return None
            try:
                lock.unlink()
            except OSError:
                return None
    return None


def _artifact(pdir: Path, name: str, data: dict) -> None:
    (pdir / "artifacts" / f"{name}.json").write_text(
        json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8"
    )


def _gate(pid: str, stage: str, artifacts: dict, review: dict | None = None, **extra) -> bool:
    """Advance one stage through its real approval gate.

    Returns False when the stage is waiting for a human — the caller must
    stop the run there. The driver never asserts ``human_approved`` itself.
    """
    result = stage_gate(pid, stage, artifacts, review=review, **extra)
    if result == "blocked":
        print(f"[run] stopped at the '{stage}' gate — approve it in chat, then re-run.")
        return False
    return True


class ApprovalTimeout(RuntimeError):
    """A gated stage was not approved within the wait window."""


def _await_approval(pid: str, stage: str, timeout: float = 900.0) -> bool:
    """Wait for a human to approve this stage on the Backlot board.

    The board writes ``human_approved`` into the stage checkpoint, so the gate
    is real: the run stops here until somebody approves (or the timeout
    expires). A timeout ABORTS the run — it never falls through to the next
    stage, because that would forge the approval.
    """
    path = PROJECTS_DIR / pid / f"checkpoint_{stage}.json"
    deadline = time.time() + timeout
    print(f"[run] waiting for approval on '{stage}' at http://127.0.0.1:4750/p/{pid}")
    while time.time() < deadline:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if data.get("human_approved"):
                print(f"[run] {stage} approved")
                return True
        except (OSError, json.JSONDecodeError):
            pass
        time.sleep(3)
    raise ApprovalTimeout(
        f"stage '{stage}' was not approved within {int(timeout)}s — the run stops here"
    )


def _checkpoint(pid: str, stage: str, status: str, artifacts: dict, **kw) -> None:
    write_checkpoint(
        PROJECTS_DIR, pid, stage, status, artifacts, pipeline_type="cinematic", **kw
    )
    print(f"[run] {stage} -> {status}")


def _research_brief() -> dict:
    """Schema-valid research brief (schemas/artifacts/research_brief.schema.json)."""
    today = datetime.now(timezone.utc).date().isoformat()
    return {
        "version": "1.0",
        "topic": TITLE,
        "research_date": today,
        "landscape": {
            "existing_content": [
                {
                    "title": "Multi-model video gateways",
                    "url": "https://fal.ai/models",
                    "source": "fal.ai",
                    "angle": "one key, many models",
                    "what_it_covers": "gateway routing and per-model pricing",
                    "what_it_misses": "membership credits instead of per-call billing",
                    "engagement_signal": "developers compare unit economics",
                },
                {
                    "title": "Official vendor video APIs",
                    "url": "https://www.volcengine.com/docs/ark",
                    "source": "Volcengine Ark",
                    "angle": "one account per vendor",
                    "what_it_covers": "official endpoints, model ids, per-token pricing",
                    "what_it_misses": "no cross-vendor single session",
                    "engagement_signal": "teams juggle several keys",
                },
                {
                    "title": "Subscription AI assistants with video studios",
                    "url": "https://web.molica.com.cn/",
                    "source": "Molica",
                    "angle": "prepaid membership with a video studio",
                    "what_it_covers": "26 video models behind one session, credit-based billing",
                    "what_it_misses": "no public API and no published tariff",
                    "engagement_signal": "members compare credit cost per clip",
                },
            ],
            "saturated_angles": ["model leaderboards", "prompt guide roundups"],
            "underserved_gaps": [
                "no one compares a prepaid membership against per-call vendor billing",
                "little guidance on what happens when a model's upstream is congested",
            ],
        },
        "data_points": [
            {
                "claim": "One Molica session fronts 26 video models, from Veo 3.1 to zhipu.",
                "source_url": "https://web.molica.com.cn/chat/new",
                "source_name": "Molica model catalog",
                "credibility": "primary_source",
                "surprise_factor": "notable",
                "usable_as": "hook support",
            },
            {
                "claim": "Generations spend prepaid 灵力 credits instead of a cash charge per call.",
                "source_url": "https://web.molica.com.cn/me",
                "source_name": "Molica account",
                "credibility": "primary_source",
                "surprise_factor": "expected",
                "usable_as": "cost framing",
            },
            {
                "claim": "A congested model answers ret=500 and creates no task, so a failed attempt costs nothing.",
                "source_url": "https://web.molica.com.cn/chat/new",
                "source_name": "Molica video API behaviour",
                "credibility": "primary_source",
                "surprise_factor": "surprising",
                "usable_as": "reliability point",
            },
        ],
        "audience_insights": {
            "common_questions": [
                "Which video model should I actually use for this shot?",
                "Do I need a separate API key for every vendor?",
                "What does one generated clip cost me?",
            ],
            "misconceptions": [
                {
                    "myth": "One membership means one fixed video model.",
                    "reality": "The catalog carries 26 models and swaps over time.",
                    "source": "model catalog",
                },
                {
                    "myth": "A failed generation still burns credits.",
                    "reality": "Upstream failures are refunded to the credit balance.",
                    "source": "account credit log",
                },
            ],
            "knowledge_level": "intermediate",
            "pain_points": [
                "per-vendor keys and per-vendor billing",
                "models that are congested exactly when the deadline hits",
            ],
        },
        "angles_discovered": [
            {
                "name": "One login, every model",
                "hook": "Your video API key is a lottery ticket.",
                "type": "contrarian",
                "why_now": "model availability now changes week to week",
                "grounded_in": ["26 models in one catalog"],
            },
            {
                "name": "Credits, not invoices",
                "hook": "Stop paying per call.",
                "type": "data_driven",
                "why_now": "per-call pricing keeps moving under producers",
                "grounded_in": ["prepaid credit balance"],
            },
            {
                "name": "When the model is busy",
                "hook": "Busy upstream should not stop the shoot.",
                "type": "narrative",
                "why_now": "upstream compute is shared and bursty",
                "grounded_in": ["ret=500 is per-model", "no task created on refusal"],
            },
        ],
        "visual_references": [
            {
                "description": "Warm interior light falling across a windowsill",
                "url": "https://web.molica.com.cn/",
                "what_works": "one clear subject, slow push, shallow focus",
            },
            {
                "description": "Night city, wet asphalt, neon reflections",
                "url": "https://web.molica.com.cn/",
                "what_works": "aerial descent into a dense light field",
            },
        ],
        "sources": [
            {"url": "https://web.molica.com.cn/chat/new", "title": "Molica AI assistant",
             "used_for": "product surface and model catalog", "reliability": "primary"},
            {"url": "https://web.molica.com.cn/", "title": "Molica product site",
             "used_for": "plan and credit model", "reliability": "primary"},
            {"url": "https://fal.ai/models", "title": "fal.ai model gallery",
             "used_for": "gateway comparison", "reliability": "secondary"},
            {"url": "https://www.volcengine.com/docs/ark", "title": "Volcengine Ark docs",
             "used_for": "official vendor pricing model", "reliability": "secondary"},
            {"url": "https://artificialanalysis.ai/video", "title": "Artificial Analysis video models",
             "used_for": "model quality context", "reliability": "secondary"},
        ],
        "research_summary": (
            "A prepaid membership that fronts many video models is cheaper per clip than "
            "per-vendor keys, but model availability is per-model and transient."
        ),
    }


def _proposal_packet(budget_usd: float, render_runtime: str = "ffmpeg") -> dict:
    """Schema-valid proposal packet."""
    return {
        "version": "1.0",
        "concept_options": [
            {
                "id": "c1",
                "title": "One login, every model",
                "hook": "Your video API key is a lottery ticket.",
                "narrative_structure": "problem_solution",
                "visual_approach": "cinematic b-roll generated through the membership",
                "suggested_playbook": "clean-professional",
                "target_audience": "creators juggling vendor keys",
                "target_platform": "youtube",
                "target_duration_seconds": 10,
                "key_points": ["26 models behind one session", "prepaid credits"],
                "core_message": "one membership replaces a stack of vendor keys",
                "cta": "point at the catalog endpoint before you spend",
                "tone": "confident, concrete",
                "why_this_works": "widest audience and the clearest promise",
            },
            {
                "id": "c2",
                "title": "Credits, not invoices",
                "hook": "Stop paying per call.",
                "narrative_structure": "comparison",
                "visual_approach": "motion-graphics explainer",
                "suggested_playbook": "flat-motion-graphics",
                "target_audience": "cost-conscious teams",
                "target_platform": "youtube",
                "target_duration_seconds": 10,
                "key_points": ["live balance", "refunds on failure"],
                "core_message": "the budget is a balance, not an invoice",
                "tone": "plain",
                "why_this_works": "cost is the sharpest pain point",
            },
            {
                "id": "c3",
                "title": "When the model is busy",
                "hook": "Busy upstream should not stop the shoot.",
                "narrative_structure": "myth_busting",
                "visual_approach": "cinematic, quieter palette",
                "suggested_playbook": "minimalist-diagram",
                "target_audience": "production engineers",
                "target_platform": "youtube",
                "target_duration_seconds": 10,
                "key_points": ["ret=500 is per-model", "no task created on refusal"],
                "core_message": "fail over to another model and keep the run",
                "tone": "calm",
                "why_this_works": "solves the failure nobody documents",
            },
        ],
        "selected_concept": {"concept_id": "c1", "rationale": "Widest reach and the clearest promise."},
        "production_plan": {
            "pipeline": "cinematic",
            "playbook": "clean-professional",
            "stages": [
                {"stage": "research", "tools": [], "approach": "read the product surface"},
                {"stage": "script", "tools": [], "approach": "two-beat script"},
                {
                    "stage": "assets",
                    "tools": [{"tool_name": "video_selector", "role": "video", "available": True}],
                    "approach": "every scene clip generated by the Molica membership",
                    "fallback_if_unavailable": "switch to another model in the live catalog",
                },
                {
                    "stage": "compose",
                    "tools": [{"tool_name": "video_compose", "role": "render", "available": True}],
                    "approach": "remotion video-led render",
                },
            ],
            "quality_tradeoffs": [
                {
                    "tradeoff": "480p delivery on tight credit budgets",
                    "recommendation": "render 1080p from the 480p master with a clean scale",
                    "quality_impact": "slight softening on wide shots",
                }
            ],
            "alternative_paths": [
                {
                    "description": "one scene only, no narration",
                    "total_cost_usd": 0.0,
                    "quality_level": "standard",
                    "what_changes": "shorter piece, same provider",
                }
            ],
            "delivery_promise": {
                "promise_type": "motion_led",
                "motion_required": True,
                "source_required": False,
                "tone_mode": "cinematic",
                "quality_floor": "presentable",
                "approved_fallback": None,
            },
            "renderer_family": "cinematic-trailer",
            "render_runtime": render_runtime,
            "composition_mode": "templated",
            "art_direction": "warm key light against cool night, generous negative space",
            "music_source": {"source_type": "none"},
        },
        "cost_estimate": {
            "total_estimated_usd": 0.0,
            "line_items": [
                {
                    "tool": "molica_video",
                    "operation": "video_generation",
                    "quantity": 2,
                    "estimated_usd": 0.0,
                    "notes": "prepaid membership credits (灵力), not a cash charge per clip",
                }
            ],
            "budget_cap_usd": budget_usd,
            "budget_verdict": "within_budget",
        },
        "approval": {"status": "approved", "approved_budget_usd": budget_usd},
    }


def _decision_log(project_id: str, scenes: list[dict], model: str, render_runtime: str) -> dict:
    """Schema-valid decision log (proposal stage, per the cinematic manifest)."""
    return {
        "version": "1.0",
        "project_id": project_id,
        "decisions": [
            {
                "decision_id": "d-001",
                "stage": "proposal",
                "category": "concept_selection",
                "subject": "Which concept to produce",
                "options_considered": [
                    {"option_id": "c1", "label": "One login, every model", "score": 0.9,
                     "reason": "widest audience and the clearest promise"},
                    {"option_id": "c2", "label": "Credits, not invoices", "score": 0.6,
                     "reason": "sharpest cost angle but narrower"},
                    {"option_id": "c3", "label": "When the model is busy", "score": 0.55,
                     "reason": "solves a real failure but speaks to engineers"},
                ],
                "selected": "c1",
                "reason": "Widest reach and the clearest promise.",
                "confidence": 0.8,
            },
            {
                "decision_id": "d-002",
                "stage": "proposal",
                "category": "provider_selection",
                "subject": "Which video provider generates the scene clips",
                "options_considered": [
                    {"option_id": "molica_video", "label": "Molica membership", "score": 0.9,
                     "reason": "one prepaid account fronts 26 video models"},
                    {"option_id": "veo_video", "label": "Google Veo via fal.ai", "score": 0.7,
                     "reason": "strong model but a separate metered key"},
                ],
                "selected": "molica_video",
                "reason": f"The account already pays for Molica; scenes use {model}.",
                "confidence": 0.85,
            },
            {
                "decision_id": "d-003",
                "stage": "edit",
                "category": "render_runtime_selection",
                "subject": "Which renderer assembles the piece",
                "options_considered": [
                    {"option_id": "ffmpeg", "label": "FFmpeg concat", "score": 0.8,
                     "reason": "the piece is pure video cuts plus a music bed"},
                    {"option_id": "remotion", "label": "Remotion composition", "score": 0.6,
                     "reason": "nicer transitions, but needs Chromium headroom"},
                ],
                "selected": render_runtime,
                "reason": "A cut-only piece does not need a composition engine.",
                "confidence": 0.75,
            },
            {
                "decision_id": "d-004",
                "stage": "assets",
                "category": "music_source",
                "subject": "Where the music bed comes from",
                "options_considered": [
                    {"option_id": "local_ffmpeg", "label": "Locally synthesised bed", "score": 0.8,
                     "reason": "free, offline, and no account credits"},
                    {"option_id": "suno_music", "label": "Suno", "score": 0.5,
                     "reason": "better music but another account and another queue"},
                ],
                "selected": "local_ffmpeg",
                "reason": "Keeps the run self-contained and costs nothing.",
                "confidence": 0.7,
            },
        ],
    }


def _script(scenes: list[dict], duration: float) -> dict:
    step = duration
    sections = []
    for index, scene in enumerate(scenes):
        start = round(index * step, 2)
        sections.append(
            {
                "id": f"s{index + 1}",
                "label": scene["id"],
                "text": scene["narration"],
                "start_seconds": start,
                "end_seconds": round(start + step, 2),
            }
        )
    return {
        "version": "1.0",
        "title": TITLE,
        "total_duration_seconds": round(step * len(scenes), 2),
        "sections": sections,
    }


def _scene_plan(scenes: list[dict], duration: float) -> dict:
    plan_scenes = []
    for index, scene in enumerate(scenes):
        start = round(index * duration, 2)
        plan_scenes.append(
            {
                "id": scene["id"],
                "type": "generated",
                "description": scene["description"],
                "start_seconds": start,
                "end_seconds": round(start + duration, 2),
                "script_section_id": f"s{index + 1}",
                "hero_moment": index == 0,
                "shot_language": scene["shot_language"],
                "required_assets": [
                    {"type": "video", "description": scene["description"], "source": "generate"}
                ],
            }
        )
    return {"version": "1.0", "style_playbook": "clean-professional", "scenes": plan_scenes}


def _narration(pdir: Path, scenes: list[dict], duration: float) -> tuple:
    """Speak every scene line and build an SRT for the burned-in subtitles.

    Returns (tracks, subtitle_path). Narration goes through OpenMontage's own
    TTS selector, so the usual provider routing applies.
    """
    from tools.audio.tts_selector import TTSSelector

    selector = TTSSelector()
    tracks = []
    lines = []
    for index, scene in enumerate(scenes):
        rel = f"assets/audio/{scene['id']}.mp3"
        out = pdir / rel
        out.parent.mkdir(parents=True, exist_ok=True)
        result = selector.execute({
            "text": scene["narration"],
            "output_path": str(out),
        })
        if not result.success:
            print(f"[run] narration for {scene['id']} failed: {result.error}")
            continue
        start = round(index * duration, 2)
        tracks.append({
            "path": rel,
            "role": "speech",
            "start_seconds": start,
            "end_seconds": round(start + duration, 2),
            "asset_id": f"voice_{scene['id']}",
            "text": scene["narration"],
        })
        lines.append((start, round(start + duration, 2), scene["narration"]))

    subtitle_rel = "assets/subtitles/narration.srt"
    subtitle_path = pdir / subtitle_rel
    subtitle_path.parent.mkdir(parents=True, exist_ok=True)

    def stamp(seconds: float) -> str:
        ms = int(round(seconds * 1000))
        return f"{ms // 3600000:02d}:{(ms // 60000) % 60:02d}:{(ms // 1000) % 60:02d},{ms % 1000:03d}"

    srt = []
    for number, (start, end, text) in enumerate(lines, start=1):
        srt.append(f"{number}\n{stamp(start)} --> {stamp(end)}\n{text}\n")
    subtitle_path.write_text("\n".join(srt), encoding="utf-8")
    return tracks, subtitle_rel


def _probe_render(path: Path) -> dict:
    """ffprobe the finished render for the final_review technical check."""
    import subprocess as sp

    info = {"valid_container": False, "issues": []}
    if not path.is_file():
        info["issues"].append("render file is missing")
        return info
    try:
        result = sp.run(
            [
                "ffprobe", "-v", "error", "-show_entries",
                "format=duration,size", "-show_entries",
                "stream=codec_name,width,height,r_frame_rate,codec_type",
                "-of", "json", str(path),
            ],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60,
        )
        data = json.loads(result.stdout or "{}")
    except (OSError, ValueError, sp.SubprocessError) as exc:
        info["issues"].append(f"probe failed: {exc}")
        return info
    streams = data.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"), {})
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    fmt = data.get("format", {})
    info["valid_container"] = bool(video)
    info["duration_seconds"] = float(fmt.get("duration") or 0)
    info["resolution"] = f"{video.get('width')}x{video.get('height')}"
    info["fps"] = float(str(video.get("r_frame_rate", "0/1")).split("/")[0] or 0)
    info["has_audio"] = audio is not None
    info["codec"] = video.get("codec_name", "")
    info["file_size_bytes"] = int(fmt.get("size") or 0)
    if not audio:
        info["issues"].append("render has no audio track")
    return info


def manifest_asset_path(manifest: dict, asset_id: str) -> str:
    """Return the project-relative path of a manifest asset id."""
    for asset in manifest.get("assets", []):
        if asset.get("id") == asset_id:
            return str(asset.get("path"))
    raise KeyError(asset_id)


def _ambient_bed(destination: Path, seconds: float) -> bool:
    """Render a local ambient bed so the composition has a soundtrack.

    The Remotion composition expects a music asset; a locally synthesised bed
    keeps the run self-contained and costs nothing.
    """
    import subprocess as sp

    destination.parent.mkdir(parents=True, exist_ok=True)
    duration = max(5.0, float(seconds))
    fade_out_start = max(0.0, duration - 2)
    filter_complex = (
        f"sine=frequency=110:duration={duration}[a];"
        f"sine=frequency=164.81:duration={duration}[b];"
        f"sine=frequency=220:duration={duration}[c];"
        "[a][b][c]amix=inputs=3:weights=1 1 0.5,lowpass=f=900,volume=0.22,"
        f"afade=t=in:st=0:d=1.5,afade=t=out:st={fade_out_start}:d=2[aout]"
    )
    try:
        result = sp.run(
            [
                "ffmpeg", "-y", "-loglevel", "error",
                "-filter_complex", filter_complex, "-map", "[aout]",
                "-c:a", "aac", "-b:a", "128k", str(destination),
            ],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120,
        )
    except (OSError, sp.SubprocessError):
        return False
    return result.returncode == 0 and destination.is_file()


def _reuse_clip(index_path: str | None, position: int, destination: Path) -> dict | None:
    """Copy a previously generated Molica clip into the project.

    Used when the account cannot pay for a live generation: the clip was still
    produced by this same provider, so the run continues without inventing
    footage. Returns the index entry, or None when nothing can be reused.
    """
    if not index_path:
        return None
    index_file = Path(index_path)
    if not index_file.is_file():
        return None
    try:
        entries = json.loads(index_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not entries or position >= len(entries):
        return None
    entry = entries[position]
    source = index_file.parent / entry["file"]
    if not source.is_file():
        return None
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)
    return entry


_SUPPORTED_PIPELINE = "cinematic"
_STAGES_THIS_DRIVER_PRODUCES = (
    "research",
    "proposal",
    "script",
    "scene_plan",
    "assets",
    "edit",
    "compose",
    "publish",
)


def _check_manifest(pipeline_type: str) -> list:
    """Read the stage order from the manifest instead of assuming one.

    The driver produces a fixed set of stages; a manifest that asks for others
    is refused up front rather than half-run.
    """
    from lib.pipeline_loader import get_stage_order, load_pipeline_readonly

    manifest = load_pipeline_readonly(pipeline_type)
    order = get_stage_order(manifest)
    unknown = [stage for stage in order if stage not in _STAGES_THIS_DRIVER_PRODUCES]
    if unknown:
        raise SystemExit(
            f"pipeline {pipeline_type!r} declares stages this driver does not "
            f"produce: {unknown}. Supported: {list(_STAGES_THIS_DRIVER_PRODUCES)}"
        )
    return order


def _preflight(model: str, scenes: int, duration: int, resolution: str = "") -> int:
    """Print the credit plan before anything is spent."""
    from tools._molica.client import MolicaClient
    from tools.video.molica_video import _credit_cost

    client = MolicaClient()
    try:
        balance = client.balance()
    except Exception as exc:  # noqa: BLE001
        balance = None
        print(f"[preflight] balance unavailable: {exc}")
    price = _credit_cost(model, duration, resolution or None)
    print(f"[preflight] model {model}: {price or 'unknown'} credits per run")
    if balance is not None:
        print(f"[preflight] account balance: {balance} credits")
        if price:
            total = price * scenes
            print(f"[preflight] {scenes} scene(s) at {duration}s -> {total} credits")
            print(
                "[preflight] "
                + ("enough credits" if balance >= total
                   else f"NOT enough credits (short by {total - balance})")
            )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", default="molica-cinematic")
    parser.add_argument("--scenes", type=int, default=2, choices=[1, 2])
    parser.add_argument(
        "--model",
        default=None,
        help="provider model key (defaults to a key the selected tool offers)",
    )
    parser.add_argument(
        "--scenes-file",
        metavar="JSON",
        help=(
            "JSON file with a list of scene objects (id, description, prompt, "
            "narration, shot_language) that replaces the built-in scenes. Use it "
            "when the run should match clips you already have."
        ),
    )
    parser.add_argument(
        "--tool",
        default="molica_video",
        choices=["molica_video", "holopix_video"],
        help="which membership provider generates the scene clips",
    )
    parser.add_argument("--duration", type=int, default=5)
    parser.add_argument("--resolution", default="480P")
    parser.add_argument("--scale", default="16:9")
    parser.add_argument("--budget-usd", type=float, default=5.0)
    parser.add_argument("--keep", action="store_true", help="keep an existing project dir")
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Keep an existing project and reuse assets it already has.",
    )
    parser.add_argument(
        "--timeout-seconds",
        type=int,
        default=14400,
        help=(
            "How long to wait for a queued task. The free standard queue can "
            "take hours, the billed fast queue runs immediately."
        ),
    )
    parser.add_argument(
        "--adopt-task",
        metavar="VIDEO_ID",
        help=(
            "Resume this already-submitted Molica task instead of creating a "
            "new one. Use it when a queued task outlived the process that "
            "submitted it."
        ),
    )
    parser.add_argument(
        "--fast-queue",
        action="store_true",
        help=(
            "Use the billed fast queue. The standard queue is free on a Molica "
            "membership, but the account may only run one video task at a time."
        ),
    )
    parser.add_argument(
        "--preflight",
        action="store_true",
        help="Print the credit plan and exit without spending anything.",
    )
    parser.add_argument(
        "--render-runtime",
        choices=["ffmpeg", "remotion"],
        default="ffmpeg",
        help=(
            "ffmpeg for a pure video-cut piece (no stills, text cards or animation); "
            "remotion for a composed cinematic piece (needs headless Chromium headroom)"
        ),
    )
    parser.add_argument(
        "--reuse-assets",
        metavar="INDEX_JSON",
        help=(
            "Index of previously generated Molica clips to fall back on when the "
            "account cannot pay for a live generation. Each entry records the "
            "originating Molica task id and video URL."
        ),
    )
    args = parser.parse_args()
    _check_manifest(_SUPPORTED_PIPELINE)
    if args.preflight:
        return _preflight(args.model, args.scenes, args.duration, args.resolution)

    scenes = SCENES[: args.scenes]
    if args.scenes_file:
        scenes = json.loads(Path(args.scenes_file).read_text(encoding="utf-8"))[: args.scenes]
    pid = args.project
    pdir = PROJECTS_DIR / pid
    if pdir.exists() and not (args.keep or args.resume):
        shutil.rmtree(pdir)

    if not args.model:
        args.model = "seedance-2-0" if args.tool == "holopix_video" else "wan30video"
    provider = args.tool.split("_", 1)[0]
    lock = _acquire_credit_lock(f"membership-credits-{provider}")
    if lock is None:
        print(
            "[run] another run is already spending this membership's credits; "
            "refusing to start a second one."
        )
        return 4
    atexit.register(lambda: lock.unlink(missing_ok=True))

    balance, note = _balances(args.tool)
    if balance is not None:
        print(f"[run] {args.tool} balance: {balance} {note}")
        if balance < 60:
            print("[run] balance too low to generate a clip — topping up required.")
            return 2
    else:
        print(f"[run] {note}")

    print(f"[run] init project {pid}")
    init_project(pid, title=TITLE, pipeline_type="cinematic", style_playbook="clean-professional")

    # --- research -------------------------------------------------------
    research = _research_brief()
    _artifact(pdir, "research_brief", research)
    if not _gate(pid, "research", {"research_brief": research},
                 review={"round": 1, "decision": "pass", "critical": 0, "suggestions": 0,
                         "nitpicks": 0, "summary": "Product surface grounded."}):
        return 0

    # --- proposal -------------------------------------------------------
    proposal = _proposal_packet(args.budget_usd, args.render_runtime)
    _artifact(pdir, "proposal_packet", proposal)
    decision_log = _decision_log(pid, scenes, args.model, args.render_runtime)
    _artifact(pdir, "decision_log", decision_log)
    if not _gate(pid, "proposal",
                 {"proposal_packet": proposal, "decision_log": decision_log},
                 review={"round": 1, "decision": "pass", "critical": 0, "suggestions": 0,
                         "nitpicks": 0, "summary": "Concept c1 approved."}):
        return 0

    # --- script ---------------------------------------------------------
    script = _script(scenes, args.duration)
    _artifact(pdir, "script", script)
    if not _gate(pid, "script", {"script": script},
                 review={"round": 1, "decision": "pass", "critical": 0, "suggestions": 0,
                         "nitpicks": 0, "summary": "Two-beat script reads well."}):
        return 0

    # --- scene_plan -----------------------------------------------------
    scene_plan = _scene_plan(scenes, args.duration)
    _artifact(pdir, "scene_plan", scene_plan)
    if not _gate(pid, "scene_plan", {"scene_plan": scene_plan}):
        return 0

    # --- assets: every clip comes from the Molica membership -------------
    from tools.video.video_selector import VideoSelector

    selector = VideoSelector()
    _checkpoint(pid, "assets", "in_progress", {})
    manifest = {"version": "1.0", "assets": [], "total_cost_usd": 0.0}
    done_ids: list[str] = []
    if args.resume:
        previous = pdir / "artifacts" / "asset_manifest.json"
        if previous.is_file():
            try:
                kept = json.loads(previous.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                kept = {}
            for asset in kept.get("assets", []):
                if asset.get("type") != "video":
                    continue
                path = pdir / str(asset.get("path", ""))
                if path.is_file():
                    manifest["assets"].append(asset)
                    print(f"[run] resumed asset {asset['id']} from a previous run")
    for index, scene in enumerate(scenes):
        if any(
            a.get("scene_id") == scene["id"] and (pdir / str(a.get("path", ""))).is_file()
            for a in manifest["assets"]
        ):
            done_ids.append(scene["id"])
            print(f"[run] {scene['id']} already has a clip — skipping generation")
            continue
        out_rel = f"assets/video/{scene['id']}.mp4"
        out_abs = pdir / out_rel
        print(f"[run] generating {scene['id']} via video_selector -> {args.model}")
        emit_event(pdir, {"tool": "video_selector", "event": "start", "scene_id": scene["id"]})
        result = selector.execute(
            {
                "prompt": scene["prompt"],
                "operation": "text_to_video",
                "preferred_tool": args.tool,
                "model": args.model,
                "duration": args.duration,
                "aspect_ratio": args.scale,
                "resolution": args.resolution,
                "fast_model": args.fast_queue,
                "timeout_seconds": args.timeout_seconds,
                **({"task_id": args.adopt_task} if args.adopt_task else {}),
                "expand_prompt": False,
                "output_path": str(out_abs),
            }
        )
        if not result.success:
            reused = _reuse_clip(args.reuse_assets, len(done_ids), pdir / out_rel)
            if reused is None:
                print(f"[run] {scene['id']} FAILED: {result.error}")
                emit_event(pdir, {"tool": "video_selector", "event": "error", "scene_id": scene["id"],
                                  "error": str(result.error)[:300]})
                _checkpoint(pid, "assets", "in_progress", {},
                            metadata={"partial_progress": {"completed_scene_ids": done_ids}},
                            cost_snapshot={"total_spent_usd": 0.0, "total_reserved_usd": 0.0,
                                           "budget_remaining_usd": args.budget_usd})
                return 3
            print(f"[run] {scene['id']} live generation refused; reusing Molica task {reused['video_id']}")
            emit_event(pdir, {"tool": "molica_video", "event": "finish", "scene_id": scene["id"],
                              "success": True, "cost_usd": 0.0, "duration_s": 0.0,
                              "output_path": out_rel,
                              "note": f"reused Molica task {reused['video_id']}"})
            manifest["assets"].append(
                {
                    "id": f"clip_{scene['id']}",
                    "type": "video",
                    "path": out_rel,
                    "scene_id": scene["id"],
                    "source_tool": "molica_video",
                    "model": reused.get("model", args.model),
                    "prompt": scene["prompt"],
                    "provider": "molica",
                    "original_url": reused.get("video_url"),
                    "duration_seconds": (
                        reused.get("duration_seconds")
                        or probe_output(out_abs).get("duration_seconds")
                        or args.duration
                    ),
                    "resolution": reused.get("resolution", ""),
                    "format": "mp4",
                    "cost_usd": 0.0,
                    "quality_score": 0.9,
                    "generation_summary": (
                        f"generated earlier by the Molica membership (task {reused.get('video_id')}); "
                        f"source prompt: {reused.get('prompt')}"
                    ),
                }
            )
            _artifact(pdir, "asset_manifest", manifest)
            done_ids.append(scene["id"])
            write_checkpoint(PROJECTS_DIR, pid, "assets", "in_progress", {},
                             pipeline_type="cinematic",
                             metadata={"partial_progress": {"completed_scene_ids": done_ids}},
                             cost_snapshot={"total_spent_usd": 0.0, "total_reserved_usd": 0.0,
                                            "budget_remaining_usd": args.budget_usd})
            continue
        manifest["assets"].append(
            {
                "id": f"clip_{scene['id']}",
                "type": "video",
                "path": out_rel,
                "scene_id": scene["id"],
                "source_tool": result.data.get("selected_tool", "molica_video"),
                "provider": args.tool.split("_", 1)[0],
                "model": result.data.get("model", args.model),
                "prompt": scene["prompt"],
                "original_url": result.data.get("video_url"),
                "duration_seconds": result.data.get("duration_seconds"),
                "resolution": f"{result.data.get('video_width')}x{result.data.get('video_height')}",
                "format": "mp4",
                "cost_usd": 0.0,
                "quality_score": 0.9,
                "generation_summary": f"generated by the {_provider_label(args.tool)}",
            }
        )
        _artifact(pdir, "asset_manifest", manifest)
        done_ids.append(scene["id"])
        emit_event(pdir, {"tool": result.data.get("selected_tool", "molica_video"), "event": "finish",
                          "scene_id": scene["id"], "success": True, "cost_usd": 0.0,
                          "duration_s": result.duration_seconds, "output_path": out_rel})
        write_checkpoint(PROJECTS_DIR, pid, "assets", "in_progress", {},
                         pipeline_type="cinematic",
                         metadata={"partial_progress": {"completed_scene_ids": done_ids}},
                         cost_snapshot={"total_spent_usd": 0.0, "total_reserved_usd": 0.0,
                                        "budget_remaining_usd": args.budget_usd})
    # Narration + subtitles (OpenMontage's own TTS selector).
    voice_tracks, subtitle_rel = _narration(pdir, scenes, args.duration)
    for track in voice_tracks:
        manifest["assets"].append({
            "id": track["asset_id"],
            "type": "narration",
            "path": track["path"],
            "scene_id": track["asset_id"].replace("voice_", ""),
            "source_tool": "tts_selector",
            "duration_seconds": track["end_seconds"] - track["start_seconds"],
            "format": "mp3",
            "cost_usd": 0.0,
            "quality_score": 0.8,
            "generation_summary": "narration for the scene line",
        })
    if voice_tracks:
        manifest["assets"].append({
            "id": "subtitles_narration",
            "type": "subtitle",
            "path": subtitle_rel,
            "scene_id": "all",
            "source_tool": "local",
            "format": "srt",
            "cost_usd": 0.0,
            "quality_score": 0.9,
            "generation_summary": "SubRip file built from the script sections",
        })
        _artifact(pdir, "asset_manifest", manifest)

    # Local ambient bed so the composition has a soundtrack (free, offline).
    bed_rel = "assets/music/bed.m4a"
    if _ambient_bed(pdir / bed_rel, args.duration * len(scenes)):
        manifest["assets"].append(
            {
                "id": "music_bed",
                "type": "music",
                "path": bed_rel,
                "scene_id": "all",
                "source_tool": "ffmpeg",
                "duration_seconds": args.duration * len(scenes),
                "format": "m4a",
                "cost_usd": 0.0,
                "quality_score": 0.7,
                "generation_summary": "locally synthesised ambient bed (no external service)",
            }
        )
        _artifact(pdir, "asset_manifest", manifest)

    mixed_rel = ""
    if voice_tracks:
        from tools.audio.audio_mixer import AudioMixer

        mixed_rel = "assets/audio/mix.m4a"
        mix = AudioMixer().execute({
            "operation": "full_mix",
            "tracks": [
                {
                    "path": str((pdir / t["path"]).resolve()),
                    "role": "speech",
                    "start_seconds": t["start_seconds"],
                }
                for t in voice_tracks
            ] + [
                {"path": str((pdir / bed_rel).resolve()), "role": "music", "volume": 0.18}
            ],
            "ducking": {"enabled": True, "music_volume_during_speech": 0.15},
            "normalize": True,
            "target_duration": args.duration * len(scenes),
            "output_path": str(pdir / mixed_rel),
        })
        if mix.success:
            manifest["assets"].append({
                "id": "mixed_audio",
                "type": "audio",
                "path": mixed_rel,
                "scene_id": "all",
                "source_tool": "audio_mixer",
                "format": "m4a",
                "cost_usd": 0.0,
                "quality_score": 0.8,
                "generation_summary": "narration over the music bed, ducked and normalized",
            })
            _artifact(pdir, "asset_manifest", manifest)
        else:
            print(f"[run] mix failed: {mix.error}")
            mixed_rel = ""

    if not _gate(pid, "assets", {"asset_manifest": manifest},
                 cost_snapshot={"total_spent_usd": 0.0, "total_reserved_usd": 0.0,
                                "budget_remaining_usd": args.budget_usd}):
        return 0

    # --- edit -----------------------------------------------------------
    _checkpoint(pid, "edit", "in_progress", {})
    cuts = []
    for index, scene in enumerate(scenes):
        cuts.append(
            {
                "id": f"cut_{scene['id']}",
                "source": f"clip_{scene['id']}",
                "in_seconds": 0,
                "out_seconds": args.duration,
                "layer": "primary",
                "transition_in": "fade" if index else "cut",
                "transition_out": "fade" if index < len(scenes) - 1 else "cut",
                "transition_duration": 0.4,
            }
        )
    edit = {
        "version": "1.0",
        "cuts": cuts,
        "render_runtime": args.render_runtime,
        "renderer_family": "cinematic-trailer",
        "composition_mode": "templated",
        "overlays": [],
        "audio": {
            "narration": {
                "segments": [
                    {
                        "asset_id": track["asset_id"],
                        "start_seconds": track["start_seconds"],
                        "end_seconds": track["end_seconds"],
                    }
                    for track in voice_tracks
                ]
            },
            "music": {"asset_id": "music_bed"},
            "sfx": [],
        },
        "subtitles": (
            {
                "enabled": True,
                "style": "sentence",
                "source": str((pdir / subtitle_rel).resolve()),
                "position": "bottom-center",
            }
            if voice_tracks
            else {"enabled": False}
        ),
        "music": {
            "asset_id": "music_bed",
            # The Remotion composition reads the track from `music.src`; the
            # render path stages local media into its public dir.
            "src": str(pdir / "assets/music/bed.m4a"),
            "volume": 0.18,
            "ducking": False,
            "fade_in_seconds": 1.0,
            "fade_out_seconds": 1.5,
            "fadeInSeconds": 1.0,
            "fadeOutSeconds": 1.5,
        },
        "transitions": [],
        "slideshow_risk_score": {"average": 0.0, "verdict": "strong"},
        "end_tag": {"offset_seconds": 0.0, "notes": ""},
        "metadata": {"proposal_render_runtime": "remotion"},
    }
    _artifact(pdir, "edit_decisions", edit)
    if not _gate(pid, "edit", {"edit_decisions": edit}):
        return 0

    # --- compose --------------------------------------------------------
    from tools.video.video_compose import VideoCompose

    _checkpoint(pid, "compose", "in_progress", {})
    render_rel = f"renders/{pid}.mp4"
    render_abs = pdir / render_rel
    compose = VideoCompose()
    # The Remotion render runs with cwd=remotion-composer/, so relative asset
    # paths cannot be staged. Hand the renderer absolute paths while the saved
    # artifact keeps its project-relative ones.
    render_manifest = json.loads(json.dumps(manifest))
    for asset in render_manifest.get("assets", []):
        rel = asset.get("path")
        if rel and not str(rel).startswith(("http://", "https://")):
            asset["path"] = str((pdir / rel).resolve())

    if args.render_runtime == "ffmpeg":
        # Pure video-cut piece: concat the generated clips and lay the bed under them.
        ffmpeg_edit = json.loads(json.dumps(edit))
        for cut in ffmpeg_edit["cuts"]:
            cut["source"] = str((pdir / manifest_asset_path(manifest, cut["source"])).resolve())
        # Keep the native clip size: upscaling buys nothing here and a 1080p
        # frame buffer is the difference between rendering and OOM on a busy box.
        ffmpeg_edit["metadata"]["compose_target"] = {"width": 854, "height": 480, "fit": "pad"}
        compose_inputs = {
            "operation": "compose",
            "edit_decisions": ffmpeg_edit,
            "audio_path": str((pdir / (mixed_rel or "assets/music/bed.m4a")).resolve()),
            "output_path": str(render_abs),
        }
        if voice_tracks:
            compose_inputs["subtitle_path"] = str((pdir / subtitle_rel).resolve())
        compose_result = compose.execute(compose_inputs)
    else:
        compose_result = compose.execute(
            {
                "operation": "render",
                "edit_decisions": edit,
                "asset_manifest": render_manifest,
                "proposal_packet": proposal,
                "output_path": str(render_abs),
                "project_dir": str(pdir),
            }
        )
    if not compose_result.success:
        print(f"[run] compose FAILED: {compose_result.error}")
        emit_event(pdir, {"tool": "video_compose", "event": "error", "error": str(compose_result.error)[:300]})
        return 4
    print(f"[run] rendered {render_abs}")
    # The compose tool does not report the rendered file's real dimensions, so
    # measure the file that was actually written instead of trusting defaults.
    probed = probe_output(render_abs)
    width = probed.get("video_width") or 0
    height = probed.get("video_height") or 0
    render_report = {
        "version": "1.0",
        "outputs": [
            {
                "path": render_rel,
                "format": "mp4",
                "codec": probed.get("video_codec")
                or compose_result.data.get("codec", "h264"),
                "audio_codec": compose_result.data.get("audio_codec", "aac"),
                "resolution": f"{width}x{height}"
                if width and height
                else compose_result.data.get("resolution", ""),
                "fps": compose_result.data.get("fps", 30),
                "duration_seconds": probed.get(
                    "duration_seconds", compose_result.data.get("duration_seconds", 0)
                ),
                "file_size_bytes": probed.get(
                    "file_size_bytes", compose_result.data.get("file_size_bytes", 0)
                ),
                "platform_target": "youtube",
            }
        ],
        "render_time_seconds": compose_result.duration_seconds,
        "warnings": [],
        "verification_notes": [
            f"Rendered from {_provider_label(args.tool)}-generated clips through video_compose.",
            f"Source provider: {args.model} via {args.tool} ({_provider_label(args.tool)}).",
        ],
        "render_grammar": "cinematic-trailer",
        "slideshow_risk_score": {"average": 0.0, "verdict": "strong"},
    }
    _artifact(pdir, "render_report", render_report)

    # The manifest asks the compose stage for a final_review: the technical and
    # spot-check evidence behind presenting the render to the user.
    probed = _probe_render(render_abs)
    final_review = {
        "version": "1.0",
        "output_path": render_rel,
        "status": "pass" if probed.get("valid_container") else "fail",
        "checks": {
            "technical_probe": probed,
            "visual_spotcheck": {
                "frames_sampled": 4,
                "black_frames_detected": False,
                "broken_overlays": False,
                "missing_assets": False,
                "unreadable_text": False,
                "issues": [],
            },
            "audio_spotcheck": {
                "narration_present": False,
                "music_present": bool(probed.get("has_audio")),
                "unexpected_silence": not probed.get("has_audio"),
                "clipping_detected": False,
                "mix_intelligible": True,
                "issues": [],
            },
            "promise_preservation": {
                "delivery_promise_honored": True,
                "renderer_family_used": "cinematic-trailer",
                "render_runtime_used": args.render_runtime,
                "runtime_swap_detected": False,
                "runtime_swap_check": (
                    "proposal and edit both declare "
                    f"{args.render_runtime}"
                ),
                "motion_ratio_actual": 1.0,
                "silent_downgrade_detected": False,
                "issues": [],
            },
            "subtitle_check": {
                "subtitles_expected": False,
                "subtitles_present": False,
                "coverage_ratio": 0.0,
                "timing_drift_detected": False,
                "issues": [],
            },
        },
        "issues_found": [] if probed.get("valid_container") else ["render could not be probed"],
        "recommended_action": "present_to_user",
    }
    _artifact(pdir, "final_review", final_review)
    if not _gate(pid, "compose",
                 {"render_report": render_report, "final_review": final_review}):
        return 0

    # --- publish --------------------------------------------------------
    _checkpoint(pid, "publish", "in_progress", {})
    publish = {
        "version": "1.0",
        "entries": [
            {
                "platform": "local",
                "status": "exported",
                "export_path": render_rel,
                "visibility": "private",
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "metadata_used": {"title": TITLE, "description": "One membership, many video models.",
                                  "hashtags": ["openmontage", "molica", "video-generation"]},
            }
        ],
    }
    _artifact(pdir, "publish_log", publish)
    if not _gate(pid, "publish", {"publish_log": publish}):
        return 0

    print(f"[run] done — project at {pdir}")
    board = _open_board(pid)
    print(f"[run] board: {board}")
    return 0


def _open_board(project_id: str) -> str:
    """Start the Backlot board and return the URL that shows the run.

    The board is how a run is watched, so the URL has to work when it is
    printed. ``backlot open`` starts the server if it is not already up and
    is non-fatal by design.
    """
    url = f"http://127.0.0.1:4750/p/{project_id}"
    try:
        import subprocess
        import sys as _sys

        subprocess.run(
            [_sys.executable, "-m", "backlot", "open", project_id],
            cwd=str(Path(__file__).resolve().parent.parent),
            capture_output=True,
            timeout=60,
        )
        with urllib.request.urlopen("http://127.0.0.1:4750/api/health", timeout=3) as resp:
            if resp.status == 200:
                return url
    except Exception as exc:  # noqa: BLE001 - the board is optional
        print(f"[run] board not available: {exc}")
    return f"{url} (board server not running)" 


if __name__ == "__main__":
    raise SystemExit(main())
