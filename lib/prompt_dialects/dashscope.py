"""DashScope wan3.0-video prompt dialect.

wan3.0-video responds best to compact Chinese cinematography prose: shot size
and camera movement first, then subject, light and the constraints the binding
actually supports. Every phrase is looked up from the same neutral tokens the
other dialects use, so switching dialects changes wording, never meaning.

Pure string rendering: no network, no provider SDK (M0, zero external API).
"""

# risk:untested reason:"dashscope dialect is covered by 38 contract tests in tests/contracts/test_shortdrama_dialects.py; thin static gate cannot link that mapping" owner:@kevin reviewed:2026-11-11

from __future__ import annotations

from typing import Any

from .neutral import structured_layers

SHOT_SIZE_PHRASES = {
    "extreme_wide": "大远景",
    "wide": "远景",
    "medium_wide": "中远景",
    "medium": "中景",
    "medium_close": "中近景",
    "close_up": "特写",
    "extreme_close_up": "大特写",
    "over_shoulder": "过肩镜头",
    "insert": "插入镜头",
    "establishing": "定场镜头",
}

CAMERA_MOVEMENT_PHRASES = {
    "static": "固定机位",
    "pan_left": "向左横摇",
    "pan_right": "向右横摇",
    "tilt_up": "向上摇镜",
    "tilt_down": "向下摇镜",
    "dolly_in": "缓慢推镜",
    "dolly_out": "缓慢拉镜",
    "tracking_left": "向左跟拍",
    "tracking_right": "向右跟拍",
    "crane_up": "上升摇臂",
    "crane_down": "下降摇臂",
    "handheld": "手持跟拍",
    "steadicam": "斯坦尼康稳定跟拍",
    "whip_pan": "快速甩镜",
    "orbital": "环绕运镜",
    "zoom_in": "缓慢变焦推近",
    "zoom_out": "缓慢变焦拉远",
    "rack_focus": "焦点转移",
}

LENS_PHRASES = {
    14: "14mm 超广角镜头",
    24: "24mm 广角镜头",
    35: "35mm 纪实感镜头",
    50: "50mm 标准镜头",
    85: "85mm 人像镜头",
    135: "135mm 中长焦镜头",
    200: "200mm 长焦镜头",
}

DEPTH_OF_FIELD_PHRASES = {
    "shallow": "浅景深，背景虚化",
    "medium": "中等景深",
    "deep": "大景深，前后景清晰",
}

LIGHTING_PHRASES = {
    "high_key": "高调明亮布光",
    "low_key": "低调暗部布光",
    "natural": "自然光",
    "golden_hour": "黄金时刻暖阳",
    "blue_hour": "蓝调时刻冷光",
    "tungsten_warm": "暖钨丝灯光",
    "neon": "霓虹色彩",
    "silhouette": "逆光剪影",
    "rim_lit": "轮廓光",
    "volumetric": "体积光",
    "overcast_soft": "阴天柔光",
}

COLOR_TEMPERATURE_PHRASES = {
    "cool": "冷色调",
    "neutral": "中性色调",
    "warm": "暖色调",
    "mixed": "冷暖对比色调",
}

#: Provider-neutral texture words, with an English fallback for free-form ones.
TEXTURE_PHRASES = {
    "grain": "颗粒感",
    "clean": "干净通透",
    "anamorphic": "变形宽银幕质感",
    "gritty": "粗粝质感",
    "ethereal": "空灵氛围",
}

_TOKEN_PHRASES: dict[str, str] = {}
_TOKEN_PHRASES.update({f"shot_size={k}": v for k, v in SHOT_SIZE_PHRASES.items()})
_TOKEN_PHRASES.update(
    {f"camera_movement={k}": v for k, v in CAMERA_MOVEMENT_PHRASES.items()}
)
_TOKEN_PHRASES.update({f"lens_mm={k}": v for k, v in LENS_PHRASES.items()})
_TOKEN_PHRASES.update(
    {f"depth_of_field={k}": v for k, v in DEPTH_OF_FIELD_PHRASES.items()}
)
_TOKEN_PHRASES.update({f"lighting_key={k}": v for k, v in LIGHTING_PHRASES.items()})
_TOKEN_PHRASES.update(
    {f"color_temperature={k}": v for k, v in COLOR_TEMPERATURE_PHRASES.items()}
)


def _phrase(token: str) -> str:
    """Map one canonical token to its wan3.0 Chinese phrasing."""
    if token in _TOKEN_PHRASES:
        return _TOKEN_PHRASES[token]
    key, _, value = token.partition("=")
    if key == "texture":
        return TEXTURE_PHRASES.get(value, value)
    # Free-form subject text passes through untouched.
    return value


def _constraint_phrase(token: str) -> str | None:
    """Map one capability constraint to prose; None means "say nothing"."""
    key, _, value = token.partition("=")
    if key == "native_audio":
        if value == "true":
            return "带原生音频与对白"
        return "无原生音频，口型需与对白匹配"
    if key == "lipsync_from_dialogue":
        return "支持对白驱动口型" if value == "true" else None
    if key == "first_frame_conditioning":
        return "以首帧图作为起始画面" if value == "true" else None
    if key == "reference_images_max":
        count = int(value)
        if count > 0:
            return f"参考图不超过 {count} 张，保持角色一致"
        return "不使用参考图"
    if key == "output_resolutions":
        return f"输出分辨率 {value}"
    return None


def _capability_value(constraints: list[str], key: str) -> str | None:
    prefix = f"{key}="
    for token in constraints:
        if token.startswith(prefix):
            return token[len(prefix):]
    return None


def render(shot_language: dict[str, Any], binding_caps: dict[str, Any]) -> str:
    """Render structured shot language as a wan3.0-style Chinese prompt."""
    layers = structured_layers(shot_language, binding_caps)

    aspect_ratio = _capability_value(layers["constraints"], "aspect_ratios")
    if aspect_ratio == "9:16":
        opening = "竖屏 9:16 电影感短剧镜头"
    elif aspect_ratio:
        opening = f"画幅 {aspect_ratio} 电影感短剧镜头"
    else:
        opening = "电影感短剧镜头"

    sentences = [opening + "。"]
    if layers["camera"]:
        sentences.append("镜头：" + "，".join(_phrase(t) for t in layers["camera"]) + "。")
    if layers["movement"]:
        sentences.append(
            "景别与运镜：" + "，".join(_phrase(t) for t in layers["movement"]) + "。"
        )
    if layers["subject"]:
        subject_bits = []
        for token in layers["subject"]:
            key, _, value = token.partition("=")
            if key == "texture":
                subject_bits.append("质感：" + TEXTURE_PHRASES.get(value, value))
            else:
                subject_bits.append(value)
        sentences.append("画面主体：" + "，".join(subject_bits) + "。")
    if layers["lighting"]:
        sentences.append(
            "光线：" + "，".join(_phrase(t) for t in layers["lighting"]) + "。"
        )
    if layers["style"]:
        sentences.append("风格：" + "，".join(_phrase(t) for t in layers["style"]) + "。")

    constraints = [
        phrase
        for phrase in (
            _constraint_phrase(token)
            for token in layers["constraints"]
            if not token.startswith("aspect_ratios=")
        )
        if phrase
    ]
    if constraints:
        sentences.append("生成约束：" + "；".join(constraints) + "。")

    return "".join(sentences)