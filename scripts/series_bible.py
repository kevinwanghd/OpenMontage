"""Series-level artifacts for 《吉鸿昌》 — research brief, proposal, decision log.

Kept separate from :mod:`jihongchang_series_run` so the episode data (which
changes per series) stays in one place and the artifact shapes stay in another.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SERIES_ID = "jihongchang-series"
HOLOPIX_CLIP_COST = 145  # measured: one 480p / 5s seedance-mini run (cheapest model)


def build_research_brief(ep01_research_path: Path) -> dict[str, Any]:
    """Build the series research brief on top of episode 1's verified brief.

    Episode 1 already carries the sourced bibliography and the checked data
    points; the series adds the multi-episode angles on top of it.
    """
    base = json.loads(ep01_research_path.read_text(encoding="utf-8"))
    base["topic"] = "《吉鸿昌》四集历史短剧系列：北望 / 红石峡 / 归国 / 绝笔"
    base["research_date"] = datetime.now(timezone.utc).date().isoformat()
    base["landscape"]["existing_content"].append({
        "title": "《吉鸿昌》第一集《北望》（本系列已交付的单集）",
        "source": "本项目 projects/jihongchang-death",
        "angle": "六镜头的一集式时间线：九一八之后到北望",
        "what_it_covers": "起点：长城与沦陷的北方",
        "what_it_misses": "多伦、解职、囚车、绝笔四段弧线",
        "engagement_signal": "已交付 30 秒成片，视觉语言被验证",
    })
    base["landscape"]["underserved_gaps"] = [
        "从塞北到牢房的一条完整时间线",
        "每一集独立的戏剧动作，而不是解说",
        "系列化的片头片尾与集数标识",
    ]
    first_source = base["sources"][0]
    base["data_points"].append({
        "claim": "把生平切成四段、每段 30 秒，观众不必一次看完，人物弧线反而更完整",
        "source_url": first_source["url"],
        "source_name": first_source["title"],
        "credibility": "secondary_source",
        "surprise_factor": "notable",
        "usable_as": "系列结构的开场钩子",
    })
    base["angles_discovered"] = [
        {
            "name": "四集时间线：北望到绝笔",
            "hook": "从长城的风，到北平牢房的枯枝。",
            "type": "narrative",
            "why_now": "第1集的单集梗概已被验证，四集能把人物弧线补全",
            "grounded_in": ["1933年察哈尔多伦", "1934年11月24日就义"],
        },
        {
            "name": "以地理为线索的流放路线",
            "hook": "长城、塞外、囚车、牢房——一个人被一步步推向终点。",
            "type": "trending",
            "why_now": "多伦、解职、押解、就义四段各自都有明确的地点",
            "grounded_in": ["红石峡", "北平陆军监狱"],
        },
        {
            "name": "绝笔的四个动作",
            "hook": "折枝、蘸血、刻字、整衣领——四行字是四段动作。",
            "type": "contrarian",
            "why_now": "把被反复解读的文本还原成可见的动作，而不是再念一遍",
            "grounded_in": ["狱中刻字", "行刑前整衣领"],
        },
    ]
    base["research_summary"] = (
        "史料集中在两处：1934年11月9日的遇刺与11月24日的就义。"
        "系列化的价值在于把中间被跳过的三年（同盟军、解职、囚车）补成独立的戏剧段落。"
    )
    return base


def build_proposal_packet(remaining_shots: int) -> dict[str, Any]:
    return {
        "version": "1.0",
        "concept_options": [
            {
                "id": "c1",
                "title": "四集纪实线：北望 / 红石峡 / 归国 / 绝笔",
                "hook": "从长城的风，到北平牢房的枯枝。",
                "narrative_structure": "timeline",
                "visual_approach": "冷调档案影像：铅灰的塞外与牢房，暖黄的会议与灯火，逐集递进到冷蓝",
                "suggested_playbook": "clean-professional",
                "target_audience": "看党史、纪录片与历史解说短片的观众",
                "target_platform": "youtube",
                "target_duration_seconds": 120,
                "key_points": ["第1集 北望", "第2集 红石峡", "第3集 归国", "第4集 绝笔"],
                "core_message": "一个人的结局，始于他在胜势中被解职的那一刻。",
                "tone": "cold, restrained",
                "why_this_works": "第1集已验证单集观感；系列化后人物有完整弧线，而不是一次性的绝笔梗概",
            },
            {
                "id": "c2",
                "title": "单集加长版：90 秒全片",
                "hook": "把一生压进一分半。",
                "narrative_structure": "story",
                "visual_approach": "同 c1，但把六镜头扩到十八镜头",
                "suggested_playbook": "clean-professional",
                "target_audience": "单次消费的短视频观众",
                "target_platform": "tiktok",
                "target_duration_seconds": 90,
                "key_points": ["塞外", "解职", "绝笔"],
                "core_message": "九十秒读完一生",
                "tone": "grave",
                "why_this_works": "一次看完不需要追更；但镜头成本是系列的 1.5 倍，算力只够一集",
            },
            {
                "id": "c3",
                "title": "图文解说：档案照片 + 字幕",
                "hook": "用史料本身说话。",
                "narrative_structure": "data_narrative",
                "visual_approach": "静态档案图与字幕卡，不生成视频",
                "suggested_playbook": "clean-professional",
                "target_audience": "考据型观众",
                "target_platform": "generic",
                "target_duration_seconds": 120,
                "key_points": ["绝笔", "时间线", "就义"],
                "core_message": "九十秒读完一生",
                "tone": "grave",
                "why_this_works": "零算力成本，但失去短剧的动作感",
            },
        ],
        "selected_concept": {
            "concept_id": "c1",
            "rationale": "用户要求拉长成系列、每集不少于 30 秒；c1 让每集都有独立的戏剧动作，且能复用第1集已验证的视觉语言",
            "modifications": ["每集独立片头卡与集数标识", "每集独立片尾卡与落款"],
        },
        "production_plan": {
            "pipeline": "cinematic",
            "playbook": "clean-professional",
            "stages": [
                {"stage": "research", "tools": [], "approach": "复用第1集已核实的史料与来源"},
                {"stage": "proposal", "tools": [], "approach": "四集结构与算力分配"},
                {"stage": "script", "tools": [], "approach": "每集六句旁白对应六个镜头"},
                {"stage": "scene_plan", "tools": [], "approach": "每集六个镜头，含镜头语言参数"},
                {"stage": "assets",
                 "tools": [{"tool_name": "video_selector", "role": "video", "available": True}],
                 "approach": "每个镜头由 Holopix 会员算力生成 seedance-2-0",
                 "fallback_if_unavailable": "MiniMax H3 配额恢复后改用 3 条 10 秒 2K 镜头"},
                {"stage": "compose",
                 "tools": [{"tool_name": "video_compose", "role": "render", "available": True}],
                 "approach": "手写 Remotion 合成（atelier），逐集只换 props"},
            ],
            "quality_tradeoffs": [
                {"tradeoff": "480p 生成 + 受限的会员算力余额",
                 "recommendation": "以 480p 母版生成，再用干净放大输出 720p",
                 "quality_impact": "草原大远景略有柔化"},
            ],
            "alternative_paths": [
                {"description": "只出第2集，剩余算力留给第3集前三条",
                 "total_cost_usd": 0.0, "quality_level": "standard",
                 "what_changes": "系列分批交付，其余集等充值"},
                {"description": "H3 配额恢复后每集改用 3 条 10 秒 2K 镜头",
                 "total_cost_usd": 0.0, "quality_level": "premium",
                 "what_changes": "调用次数减半，画质从 480p 提到 2K"},
            ],
            "delivery_promise": {
                "promise_type": "motion_led",
                "motion_required": True,
                "source_required": False,
                "tone_mode": "cinematic",
                "quality_floor": "presentable",
                "approved_fallback": None,
            },
            "renderer_family": "documentary-montage",
            "render_runtime": "remotion",
            "composition_mode": "atelier",
            "art_direction": "projects\\jihongchang-death\\art-direction.md（逐集复用）",
            "music_source": {"source_type": "none", "mood_direction": "低频弦乐与风声的氛围床，不喧宾夺主"},
            "voice_selection": {
                "provider": "doubao_tts",
                "rationale": "环境里唯一配置好的中文人声（与第1集一致）",
                "delivery_style": "冷静克制的纪实旁白，不煽情",
                "pacing_policy": "每句一句一顿，句末下沉，留半秒静默",
                "sample_approval_required": False,
                "estimated_cost_usd": 0.0,
            },
            "provider_rankings": {
                "video": [
                    {"tool_name": "holopix_video", "provider": "Holopix", "weighted_score": 0.9,
                     "task_fit": 0.9,
                     "output_quality": 0.7,
                     "explanation": "会员算力已付费，余额 2775，够出第2集"},
                    {"tool_name": "minimax_video", "provider": "MiniMax", "weighted_score": 0.75,
                     "task_fit": 0.85,
                     "output_quality": 0.9,
                     "explanation": "当前直连返回 402：Token Plan 用量上限，配额恢复后首选"},
                    {"tool_name": "molica_video", "provider": "Molica", "weighted_score": 0.2,
                     "task_fit": 0.6, "output_quality": 0.9,
                     "explanation": "余额 102 灵力，H3 起步 157 灵力，用不起"},
                ],
                "image": [
                    {"tool_name": "none", "provider": "local", "weighted_score": 0.5,
                     "task_fit": 0.1, "output_quality": 0.0,
                     "explanation": "片头片尾由手写合成排版，不生成图片"},
                ],
                "tts": [
                    {"tool_name": "doubao_tts", "provider": "Doubao", "weighted_score": 0.9,
                     "task_fit": 0.9, "output_quality": 0.85,
                     "explanation": "环境里已配置 DOUBAO 语音密钥"},
                ],
                "music": [
                    {"tool_name": "local_ffmpeg", "provider": "local", "weighted_score": 0.8,
                     "task_fit": 0.8, "output_quality": 0.6,
                     "explanation": "不消耗任何会员额度"},
                ],
            },
            "decision_log_ref": "artifacts/decision_log.json",
        },
        "cost_estimate": {
            "total_estimated_usd": 0.0,
            "line_items": [
                {"tool": "holopix_video", "operation": "video_generation", "quantity": remaining_shots,
                 "estimated_usd": 0.0,
                 "notes": f"会员算力（非美元计费）：{remaining_shots} 条 × {HOLOPIX_CLIP_COST} 算力"},
                {"tool": "doubao_tts", "operation": "text_to_speech", "quantity": remaining_shots,
                 "estimated_usd": 0.0, "notes": "旁白配音"},
                {"tool": "local_ffmpeg", "operation": "audio_mix", "quantity": 3,
                 "estimated_usd": 0.0, "notes": "混音与本地音乐床"},
            ],
            "budget_cap_usd": 5.0,
            "budget_verdict": "within_budget",
            "savings_options": [
                "只出第2集（1734 算力），余额 1041 算力留给第3集前三条",
                "H3 配额恢复后每集改用 3 条 10 秒 2K 镜头",
                "充值 Holopix 算力：每集 1734 算力",
            ],
        },
        "approval": {
            "status": "pending",
            "user_notes": "",
            "approved_budget_usd": 5.0,
        },
    }


def build_decision_log() -> dict[str, Any]:
    return {
        "version": "1.0",
        "project_id": SERIES_ID,
        "decisions": [
            {
                "decision_id": "d-s01",
                "stage": "proposal",
                "category": "concept_selection",
                "subject": "单集还是系列",
                "options_considered": [
                    {"option_id": "s1", "label": "四集纪实线", "score": 0.9,
                     "reason": "每集独立的戏剧动作，人物弧线完整"},
                    {"option_id": "s2", "label": "单集 90 秒加长版", "score": 0.6,
                     "reason": "一次看完，但算力只够一集且中间段落被压缩"},
                    {"option_id": "s3", "label": "图文解说", "score": 0.4,
                     "reason": "零算力成本，但失去短剧的动作感"},
                ],
                "selected": "s1",
                "reason": "用户要求每集不少于 30 秒并做成系列",
                "user_visible": True,
                "user_approved": False,
                "confidence": 0.9,
            },
            {
                "decision_id": "d-s02",
                "stage": "proposal",
                "category": "playbook_override",
                "subject": "逐集复用第1集的手写合成还是重新模板化",
                "options_considered": [
                    {"option_id": "atelier_reuse", "label": "复用第1集手写合成，只换 props",
                     "score": 0.9, "reason": "片头片尾与字幕语言已验证，省算力且风格一致"},
                    {"option_id": "templated", "label": "模板化 cinematic-trailer",
                     "score": 0.4, "reason": "没有中文字幕与片头片尾卡"},
                ],
                "selected": "atelier_reuse",
                "reason": "沿用第1集已交付的视觉语言",
                "user_visible": True,
                "user_approved": False,
                "confidence": 0.9,
            },
            {
                "decision_id": "d-s03",
                "stage": "assets",
                "category": "capability_extension",
                "subject": "镜头生成线路",
                "options_considered": [
                    {"option_id": "holopix_video", "label": "Holopix seedance-2-0",
                     "score": 0.9, "reason": "余额 2775 算力，5 秒一条正好对齐每集六镜头"},
                    {"option_id": "minimax_video", "label": "MiniMax H3",
                     "score": 0.5, "reason": "画质最好，但直连返回 402（Token Plan 用量上限）"},
                    {"option_id": "molica_video", "label": "Molica H3 代理",
                     "score": 0.2, "reason": "余额 102 灵力，H3 起步 157 灵力"},
                ],
                "selected": "holopix_video",
                "reason": "H3 配额不可用时的唯一可行线路",
                "user_visible": True,
                "user_approved": False,
                "confidence": 0.85,
            },
        ],
    }
