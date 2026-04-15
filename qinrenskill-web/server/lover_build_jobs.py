# -*- coding: utf-8 -*-
"""
恋人流水线的异步任务管理（类似 build_jobs.py 但恋人专属）。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Dict, Optional, Callable

from app.deepseek_client import DeepSeekClient
from app.pipeline import (
    CharacterJSON,
    _split_aliases,
    run_snippet_extractor,
    render_roleplay_system_prompt_markdown,
)


@dataclass
class LoverBuildState:
    job_id: str
    character_name: str
    done: bool = False
    error: str = ""
    message: str = ""
    percent: int = 0
    system_prompt: str = ""
    extraction_output: str = ""


_jobs: Dict[str, LoverBuildState] = {}
_lock = threading.Lock()


def _new_id() -> str:
    import uuid
    return uuid.uuid4().hex[:12]


def create_lover_job(name: str) -> str:
    job_id = _new_id()
    with _lock:
        _jobs[job_id] = LoverBuildState(job_id=job_id, character_name=name)
    return job_id


def get_lover_job(job_id: str) -> Optional[LoverBuildState]:
    with _lock:
        return _jobs.get(job_id)


def _update_job(job_id: str, **kwargs) -> None:
    with _lock:
        job = _jobs.get(job_id)
        if job:
            for k, v in kwargs.items():
                setattr(job, k, v)


def run_lover_novel_build_task(
    *,
    job_id: str,
    api_key: str,
    novel_path: str,
    character_name: str,
    aliases_text: str,
    chat_rules: str = "",
    model: str = "",
    output_dir: str = "",
    status_cb: Optional[Callable[[str], None]] = None,
) -> None:
    """
    从小说提取恋人特质。复用现有的片段抽取 + LLM 归纳，
    但使用恋人专属的系统提示词来引导模型关注恋爱维度。
    """
    import os

    resolved_model = (model or "").strip() or os.environ.get("PIPELINE_MODEL", "deepseek-reasoner")

    def _cb(msg: str) -> None:
        _update_job(job_id, message=msg)
        if status_cb:
            status_cb(msg)

    try:
        aliases = _split_aliases(aliases_text)
        client = DeepSeekClient(api_key=api_key, timeout_s=180, max_retries=4)

        _cb("步骤 1/3：全书抽取角色片段…")
        _update_job(job_id, percent=10)

        # 复用现有片段抽取
        snippets_path = run_snippet_extractor(
            novel_path=novel_path,
            character_name=character_name,
            aliases=aliases,
            output_dir=output_dir or os.path.join(os.path.dirname(__file__), "..", "artifacts"),
            context_lines=20,
            require_quote=False,
        )

        _cb("步骤 2/3：调用模型归纳恋人特质（分批）…")
        _update_job(job_id, percent=30)

        # 使用恋人专属的系统提示词（强调浪漫关系维度）
        cj = _build_lover_character_json(
            client=client,
            model=resolved_model,
            character_name=character_name,
            snippets_md_path=snippets_path,
            status_cb=_cb,
        )

        _cb("步骤 2/3：渲染恋人档案…")
        _update_job(job_id, percent=70)

        # 编译为恋人档案
        lover_archive = _render_lover_archive_from_cj(cj, character_name)

        _cb("步骤 3/3：生成恋人扮演指令…")
        _update_job(job_id, percent=85)

        # 编译为系统提示词
        system_prompt = render_roleplay_system_prompt_markdown(
            client=client,
            model=resolved_model,
            character_archive_md=lover_archive,
        )

        # 追加用户规则
        rules = (chat_rules or "").strip()
        if rules:
            system_prompt += "\n\n## 对话补充规则（用户指定）\n\n" + rules

        _update_job(
            job_id,
            done=True,
            percent=100,
            system_prompt=system_prompt,
            extraction_output=lover_archive,
            message="完成",
        )
    except Exception as e:
        _update_job(job_id, done=True, error=str(e), message=f"失败：{e}")


def _build_lover_character_json(
    *,
    client,
    model: str,
    character_name: str,
    snippets_md_path: str,
    status_cb,
) -> CharacterJSON:
    """
    复用现有的分批归纳逻辑，但系统提示词聚焦恋爱维度。
    """
    from app.pipeline import (
        _read_text,
        _parse_snippets,
        _batch_by_chars,
        _extract_items_from_lines,
        merge_character_json,
        CharacterJSON,
        _env_batch_max_chars,
    )

    md = _read_text(snippets_md_path)
    blocks = _parse_snippets(md)
    if not blocks:
        raise RuntimeError("没有抽取到任何片段。")

    batches = _batch_by_chars(blocks, max_chars=_env_batch_max_chars())
    status_cb(f"片段总数：{len(blocks)}；将分 {len(batches)} 批调用模型归纳。")

    # 恋人专属系统提示词：强调恋爱关系维度
    system = (
        "你是严谨的小说人物恋爱特质信息抽取器。你必须只依据用户提供的片段做总结，"
        "禁止补完未写明设定。重点关注该人物的**恋爱相关特质**：如何表达爱意、"
        "与人亲密互动的方式、情感表达风格、冲突处理模式、日常关心方式等。"
        "输出必须是逐行条目格式，不要输出 JSON、不要输出解释性段落。"
    )

    base = CharacterJSON(character_name=character_name)
    for idx, batch in enumerate(batches, start=1):
        status_cb(f"归纳中：第 {idx}/{len(batches)} 批…")
        user = (
            f"目标人物：{character_name}\n\n"
            "请从以下片段中抽取信息，重点关注**恋爱/亲密关系**相关特质。\n\n"
            "输出格式（逐行条目格式，每行一条，使用 | 分隔）：\n\n"
            "VOICE|<说话方式/口吻/称呼>|<evidence>\n"
            "PERSONA|<人格特质>|<confidence:高/中/低>|<evidence>\n"
            "GOAL|<目标/追求>|<confidence:高/中/低>|<evidence>\n"
            "RELATION|<关系模式/互动方式>|<confidence:高/中/低>|<evidence>\n"
            "CAPABILITY|<能力/特长>|<confidence:高/中/低>|<evidence>\n"
            "BOUNDARY|<禁区/不做的事>|<evidence>\n"
            "FACT|<明确的事实>|<evidence>\n"
            "OPENQ|<待确认的问题>\n"
            "DONTINFER|<禁止补完的设定>\n\n"
            "特别注意：关注该人物在恋爱/亲密关系中的表现，如：\n"
            "- 如何表达爱意和关心\n"
            "- 情感表达风格（直接/含蓄/幽默/诗意）\n"
            "- 冲突时的反应和处理方式\n"
            "- 日常互动模式（主动/被动/平等）\n"
            "- 亲密感和边界感\n\n"
            "规则：\n"
            "- evidence 必须是短摘录，不要复制大段。\n"
            "- 不确定写 OPENQ 或 DONTINFER。\n"
            "- 只输出条目行，不要写标题或总结。\n\n"
            f"片段如下：\n{batch}\n"
        )
        resp = client.chat_completions(
            model=model,
            system=system,
            user=user,
            max_tokens=2200,
            temperature=0.1,
        )
        delta = _extract_items_from_lines(resp.content)
        delta.character_name = character_name
        merge_character_json(base, delta)
    return base


def _render_lover_archive_from_cj(cj: CharacterJSON, name: str) -> str:
    """将 CharacterJSON 转为恋人档案风格的 markdown。"""
    def _fmt(items, item_key="item", evidence_key="evidence"):
        if not items:
            return "- （素材不足）"
        lines = []
        for it in items:
            item = it.get(item_key, "")
            evidence = it.get(evidence_key, "")
            conf = it.get("confidence", "")
            parts = [item]
            if conf:
                parts.append(f"[{conf}]")
            if evidence:
                parts.append(f"证据：{evidence}")
            lines.append("- " + " ".join(parts))
        return "\n".join(lines)

    # Python < 3.12：外层 f-string 的 {…} 内不能含反斜杠，故不能写 {''.join(f'- {q}\n' …)}
    open_questions_md = (
        "".join(f"- {q}\n" for q in cj.open_questions)
        if cj.open_questions
        else "- （无）"
    )
    do_not_infer_md = (
        "".join(f"- {x}\n" for x in cj.do_not_infer)
        if cj.do_not_infer
        else "- （无）"
    )

    return f"""# 恋人特征提取结果（从小说素材）

## Meta

- 目标人物：{cj.character_name or name}
- 来源：小说文本片段提取
- 注意：以下为基于片段的推断，置信度已标注。

## 1. Voice

{_fmt(cj.voice)}

## 2. Persona

{_fmt(cj.persona)}

## 3. Goals

{_fmt(cj.goals)}

## 4. Relationships

{_fmt(cj.relationships)}

## 5. Capabilities

{_fmt(cj.capabilities)}

## 6. HardBoundaries

{_fmt(cj.hard_boundaries)}

## 7. AllowedFacts

{_fmt(cj.allowed_facts, item_key="fact")}

## 8. OpenQuestions

{open_questions_md}

## 9. DoNotInfer

{do_not_infer_md}
"""
