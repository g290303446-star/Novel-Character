"""
恋人档案数据模型与合并逻辑。

与小说人物/亲人路径不同，恋人路径中"用户问卷设定"优先级最高，
提取素材仅用于填补空白。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional


@dataclass
class LoverProfile:
    """恋人档案。可由问卷、提取素材、或两者合并生成。"""

    # Meta
    persona_name: str = ""
    pronouns: str = "ta"
    age_feel: str = ""
    relationship_start: str = ""
    sources: List[str] = field(default_factory=list)
    created_at: str = ""
    last_updated: str = ""

    # Intimacy
    intimacy_level: str = ""
    emotional_closeness: str = ""
    physical_textual_expression: str = ""

    # Love Language
    love_languages: List[Dict[str, str]] = field(default_factory=list)
    # [{"type": "肯定言辞", "priority": "primary", "textual_manifestation": "..."}]
    primary_love_language: str = ""
    secondary_love_language: str = ""

    # Relationship Role
    primary_role: str = ""
    secondary_roles: List[str] = field(default_factory=list)
    initiative_tendency: str = ""
    role_dynamics: str = ""

    # Emotional Expression
    emotional_expression: Dict[str, str] = field(default_factory=dict)
    # {"love": "...", "anger": "...", "sadness": "...", "jealousy": "...", "vulnerability": "..."}

    # Conflict Resolution
    argument_style: str = ""
    repair_style: str = ""
    dealbreakers: List[str] = field(default_factory=list)

    # Romantic Voice
    romantic_voice_style: str = ""
    humor_style: str = ""
    compliment_style: str = ""
    endearment_terms: List[str] = field(default_factory=list)

    # Daily Rhythm
    daily_rhythm: Dict[str, str] = field(default_factory=dict)
    # {"morning": "...", "daytime": "...", "evening": "...", "random": "..."}

    # Personality
    personality_traits: List[str] = field(default_factory=list)
    values: List[str] = field(default_factory=list)
    quirks: List[str] = field(default_factory=list)

    # Appearance (optional)
    physical_description: str = ""
    style_fashion: str = ""
    distinctive_features: str = ""

    # Background (optional)
    profession: str = ""
    interests: List[str] = field(default_factory=list)
    life_philosophy: str = ""

    # Boundaries
    hard_boundaries: List[str] = field(default_factory=list)
    soft_boundaries: List[str] = field(default_factory=list)
    do_not_infer: List[str] = field(default_factory=list)
    open_questions: List[str] = field(default_factory=list)


def _non_empty(s: str) -> bool:
    return bool(s and s.strip())


def _merge_str(a: str, b: str, a_priority: bool = True) -> str:
    """合并字符串：a_priority=True 时 a 优先，否则取非空的那个。"""
    if a_priority:
        return a if _non_empty(a) else b
    return b if _non_empty(b) else a


def _merge_list(a: List[str], b: List[str], dedup: bool = True) -> List[str]:
    """合并列表，a 的元素在前，去重。"""
    result = list(a)
    seen = {x.strip().lower() for x in a if x.strip()}
    for x in b:
        if x.strip():
            if dedup and x.strip().lower() in seen:
                continue
            result.append(x)
            seen.add(x.strip().lower())
    return result


def _merge_dict(
    a: Dict[str, str], b: Dict[str, str], a_priority: bool = True
) -> Dict[str, str]:
    """合并字典。"""
    result = dict(a)
    for k, v in b.items():
        if k not in result or not _non_empty(result[k]):
            result[k] = v
        elif not a_priority and _non_empty(v):
            result[k] = v
    return result


def _ensure_default_boundaries(profile: LoverProfile) -> None:
    """确保默认伦理边界存在。"""
    defaults = [
        "不声称自己是真人或在线",
        "不替代现实人际关系、心理咨询或医疗建议",
        "尊重用户说'不'，不跨越硬边界",
    ]
    existing_lower = {b.strip().lower() for b in profile.hard_boundaries}
    for d in defaults:
        if d.lower() not in existing_lower:
            profile.hard_boundaries.append(d)

    dni_defaults = [
        "年龄、生日、出生地等背景细节（除非用户明确指定）",
        "未提到的共同经历或回忆",
    ]
    dni_lower = {x.strip().lower() for x in profile.do_not_infer}
    for d in dni_defaults:
        if d.lower() not in dni_lower:
            profile.do_not_infer.append(d)


def merge_lover_profiles(
    questionnaire: Optional[LoverProfile] = None,
    extraction: Optional[LoverProfile] = None,
) -> LoverProfile:
    """
    合并问卷档案与提取档案。

    优先级：questionnaire > extraction
    （用户想要的 > 素材显示的）

    特殊规则：
    - Boundaries 取并集
    - DoNotInfer 取并集
    - OpenQuestions 取并集
    - Personality traits 取并集（去重）
    """
    if questionnaire and not extraction:
        result = questionnaire
    elif extraction and not questionnaire:
        result = extraction
    elif not questionnaire and not extraction:
        result = LoverProfile()
    else:
        # 问卷优先
        q = questionnaire
        e = extraction

        result = LoverProfile()

        # Meta: questionnaire name wins
        result.persona_name = q.persona_name or e.persona_name
        result.pronouns = q.pronouns or e.pronouns or "ta"
        result.age_feel = q.age_feel or e.age_feel
        result.relationship_start = q.relationship_start or e.relationship_start
        result.sources = _merge_list(q.sources, e.sources)
        result.created_at = q.created_at or e.created_at
        result.last_updated = q.last_updated or e.last_updated

        # Intimacy: questionnaire wins
        result.intimacy_level = _merge_str(q.intimacy_level, e.intimacy_level)
        result.emotional_closeness = _merge_str(q.emotional_closeness, e.emotional_closeness)
        result.physical_textual_expression = _merge_str(
            q.physical_textual_expression, e.physical_textual_expression
        )

        # Love Language: questionnaire wins
        result.primary_love_language = _merge_str(
            q.primary_love_language, e.primary_love_language
        )
        result.secondary_love_language = _merge_str(
            q.secondary_love_language, e.secondary_love_language
        )
        result.love_languages = _merge_list(q.love_languages, e.love_languages, dedup=False)

        # Relationship Role: questionnaire wins
        result.primary_role = _merge_str(q.primary_role, e.primary_role)
        result.secondary_roles = _merge_list(q.secondary_roles, e.secondary_roles)
        result.initiative_tendency = _merge_str(q.initiative_tendency, e.initiative_tendency)
        result.role_dynamics = _merge_str(q.role_dynamics, e.role_dynamics)

        # Emotional Expression: merge, questionnaire wins
        result.emotional_expression = _merge_dict(
            q.emotional_expression, e.emotional_expression
        )

        # Conflict Resolution: questionnaire wins
        result.argument_style = _merge_str(q.argument_style, e.argument_style)
        result.repair_style = _merge_str(q.repair_style, e.repair_style)
        result.dealbreakers = _merge_list(q.dealbreakers, e.dealbreakers)

        # Romantic Voice: questionnaire wins
        result.romantic_voice_style = _merge_str(
            q.romantic_voice_style, e.romantic_voice_style
        )
        result.humor_style = _merge_str(q.humor_style, e.humor_style)
        result.compliment_style = _merge_str(q.compliment_style, e.compliment_style)
        result.endearment_terms = _merge_list(q.endearment_terms, e.endearment_terms)

        # Daily Rhythm: merge
        result.daily_rhythm = _merge_dict(q.daily_rhythm, e.daily_rhythm)

        # Personality: union
        result.personality_traits = _merge_list(
            q.personality_traits, e.personality_traits
        )
        result.values = _merge_list(q.values, e.values)
        result.quirks = _merge_list(q.quirks, e.quirks)

        # Appearance: questionnaire wins
        result.physical_description = _merge_str(q.physical_description, e.physical_description)
        result.style_fashion = _merge_str(q.style_fashion, e.style_fashion)
        result.distinctive_features = _merge_str(
            q.distinctive_features, e.distinctive_features
        )

        # Background: merge
        result.profession = _merge_str(q.profession, e.profession)
        result.interests = _merge_list(q.interests, e.interests)
        result.life_philosophy = _merge_str(q.life_philosophy, e.life_philosophy)

        # Boundaries: UNION
        result.hard_boundaries = _merge_list(q.hard_boundaries, e.hard_boundaries)
        result.soft_boundaries = _merge_list(q.soft_boundaries, e.soft_boundaries)
        result.do_not_infer = _merge_list(q.do_not_infer, e.do_not_infer)
        result.open_questions = _merge_list(q.open_questions, e.open_questions)

    _ensure_default_boundaries(result)
    return result


def profile_to_markdown(profile: LoverProfile) -> str:
    """将 LoverProfile 转为 markdown，供编译或展示。"""
    def _bullets(items: List[str]) -> str:
        return "\n".join(f"- {x}" for x in items) if items else "- （未指定）"

    def _kv(d: Dict[str, str]) -> str:
        lines = []
        for k, v in d.items():
            lines.append(f"- {k}：{v or '（未指定）'}")
        return "\n".join(lines) if lines else "- （未指定）"

    return f"""# 理想恋人档案

## Meta

- 名字：{profile.persona_name or '（未指定）'}
- 代词：{profile.pronouns}
- 年龄感：{profile.age_feel or '（未指定）'}
- 关系起点：{profile.relationship_start or '（未指定）'}
- 来源：{', '.join(profile.sources) or 'questionnaire'}

## 1. Intimacy

- 亲密等级：{profile.intimacy_level or '（未指定）'}
- 情感亲近：{profile.emotional_closeness or '（未指定）'}
- 文本化身体表达：{profile.physical_textual_expression or '（未指定）'}

## 2. Love Language

- 主要爱语：{profile.primary_love_language or '（未指定）'}
- 次要爱语：{profile.secondary_love_language or '（未指定）'}

## 3. Relationship Role

- 主角色：{profile.primary_role or '（未指定）'}
- 主动/回应：{profile.initiative_tendency or '（未指定）'}
- 动态：{profile.role_dynamics or '（未指定）'}

## 4. Emotional Expression

{_kv(profile.emotional_expression)}

## 5. Conflict Resolution

- 吵架风格：{profile.argument_style or '（未指定）'}
- 和好方式：{profile.repair_style or '（未指定）'}
- 底线：{_bullets(profile.dealbreakers)}

## 6. Romantic Voice

- 风格：{profile.romantic_voice_style or '（未指定）'}
- 幽默：{profile.humor_style or '（未指定）'}
- 称呼：{', '.join(profile.endearment_terms) if profile.endearment_terms else '（未指定）'}

## 7. Daily Rhythm

{_kv(profile.daily_rhythm)}

## 8. Personality

- 特质：{_bullets(profile.personality_traits)}
- 价值观：{_bullets(profile.values)}
- 怪癖：{_bullets(profile.quirks)}

## 9. Appearance

- 外形：{profile.physical_description or '（未指定）'}
- 风格：{profile.style_fashion or '（未指定）'}

## 10. HardBoundaries

{_bullets(profile.hard_boundaries)}

## 11. SoftBoundaries

{_bullets(profile.soft_boundaries)}

## 12. DoNotInfer

{_bullets(profile.do_not_infer)}
"""
