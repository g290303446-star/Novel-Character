import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, Iterable, List, Optional, Tuple

from .deepseek_client import DeepSeekClient, DeepSeekError


REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SCRIPTS_EXTRACTOR = os.path.join(REPO_ROOT, "scripts", "extract_character_snippets.py")
ARTIFACTS_DIR = os.path.join(REPO_ROOT, "artifacts")


def _env_batch_max_chars() -> int:
    """
    每批「片段正文」合并上限（按 Python str 字符数计，非 token）。
    模型上下文为 token 维度；中文约 1～2 token/字，需为 system/指令/输出预留额度，勿单批贴满 128K。
    """
    try:
        v = int(os.environ.get("BATCH_MAX_CHARS", "12000"))
    except ValueError:
        v = 12000
    return max(3000, min(v, 200_000))

CHAR_ARCHIVE_TEMPLATE_PATH = os.path.join(
    REPO_ROOT,
    ".cursor",
    "skills",
    "extract-fiction-character",
    "templates",
    "character-archive.md",
)
ROLEPLAY_SYSTEM_TEMPLATE_PATH = os.path.join(
    REPO_ROOT,
    ".cursor",
    "skills",
    "fiction-persona",
    "templates",
    "roleplay-system-prompt.md",
)


def _read_text(path: str) -> str:
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def _slugify(s: str) -> str:
    s = s.strip()
    if not s:
        return "角色"
    s = re.sub(r"\s+", "-", s)
    s = re.sub(r"[^\u4e00-\u9fffA-Za-z0-9\-_.]+", "", s)
    return s[:60] or "角色"


def _split_aliases(text: str) -> List[str]:
    out: List[str] = []
    for line in text.splitlines():
        for part in re.split(r"[，,;；\s]+", line.strip()):
            if part:
                out.append(part)
    # stable unique
    seen = set()
    uniq = []
    for x in out:
        if x not in seen:
            seen.add(x)
            uniq.append(x)
    return uniq


def _parse_snippets(md: str) -> List[str]:
    """
    从脚本输出 markdown 中提取每个片段（含头部行）。
    """
    blocks: List[str] = []
    cur: List[str] = []
    for line in md.splitlines():
        if re.match(r"^---\s*片段\s+\d+\s+\(行\s+\d+-\d+\)\s+---\s*$", line):
            if cur:
                blocks.append("\n".join(cur).strip())
                cur = []
        cur.append(line)
    if cur:
        blocks.append("\n".join(cur).strip())
    # 去掉文件头部 Meta（没有片段头的那块）
    blocks = [b for b in blocks if b.startswith("--- 片段 ")]
    return blocks


def _batch_by_chars(blocks: List[str], max_chars: int) -> List[str]:
    batches: List[str] = []
    cur: List[str] = []
    cur_len = 0
    for b in blocks:
        if not cur:
            cur = [b]
            cur_len = len(b)
            continue
        if cur_len + len(b) + 2 <= max_chars:
            cur.append(b)
            cur_len += len(b) + 2
        else:
            batches.append("\n\n".join(cur))
            cur = [b]
            cur_len = len(b)
    if cur:
        batches.append("\n\n".join(cur))
    return batches


@dataclass
class CharacterJSON:
    character_name: str = ""
    voice: List[Dict[str, str]] = field(default_factory=list)  # {item, evidence}
    persona: List[Dict[str, str]] = field(default_factory=list)  # {item, confidence, evidence}
    goals: List[Dict[str, str]] = field(default_factory=list)  # {item, confidence, evidence}
    relationships: List[Dict[str, str]] = field(default_factory=list)  # {item, confidence, evidence}
    capabilities: List[Dict[str, str]] = field(default_factory=list)  # {item, confidence, evidence}
    hard_boundaries: List[Dict[str, str]] = field(default_factory=list)  # {item, evidence}
    open_questions: List[str] = field(default_factory=list)
    do_not_infer: List[str] = field(default_factory=list)
    allowed_facts: List[Dict[str, str]] = field(default_factory=list)  # {fact, evidence}


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s.strip())


def _merge_list_of_dicts(dst: List[Dict[str, str]], src: List[Dict[str, str]], key: str) -> None:
    seen = {_norm(x.get(key, "")) for x in dst if x.get(key)}
    for x in src:
        k = _norm(x.get(key, ""))
        if not k or k in seen:
            continue
        seen.add(k)
        dst.append(x)


def _merge_list_of_str(dst: List[str], src: List[str]) -> None:
    seen = {_norm(x) for x in dst}
    for x in src:
        k = _norm(x)
        if not k or k in seen:
            continue
        seen.add(k)
        dst.append(x)


def merge_character_json(base: CharacterJSON, delta: CharacterJSON) -> CharacterJSON:
    if not base.character_name and delta.character_name:
        base.character_name = delta.character_name
    _merge_list_of_dicts(base.voice, delta.voice, "item")
    _merge_list_of_dicts(base.persona, delta.persona, "item")
    _merge_list_of_dicts(base.goals, delta.goals, "item")
    _merge_list_of_dicts(base.relationships, delta.relationships, "item")
    _merge_list_of_dicts(base.capabilities, delta.capabilities, "item")
    _merge_list_of_dicts(base.hard_boundaries, delta.hard_boundaries, "item")
    _merge_list_of_str(base.open_questions, delta.open_questions)
    _merge_list_of_str(base.do_not_infer, delta.do_not_infer)
    _merge_list_of_dicts(base.allowed_facts, delta.allowed_facts, "fact")
    return base


def _extract_items_from_lines(text: str) -> CharacterJSON:
    """
    解析模型输出的“逐行条目格式”，避免 JSON 解析脆弱性。

    期望格式（每行一条，使用 | 分隔）：

    - VOICE|<item>|<evidence>
    - PERSONA|<item>|<confidence:高/中/低>|<evidence>
    - GOAL|<item>|<confidence>|<evidence>
    - RELATION|<item>|<confidence>|<evidence>
    - CAPABILITY|<item>|<confidence>|<evidence>
    - BOUNDARY|<item>|<evidence>
    - FACT|<fact>|<evidence>
    - OPENQ|<question>
    - DONTINFER|<item>
    """
    cj = CharacterJSON()
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("```"):
            continue
        parts = [p.strip() for p in line.split("|")]
        tag = parts[0].upper() if parts else ""

        try:
            if tag == "VOICE" and len(parts) >= 3:
                cj.voice.append({"item": parts[1], "evidence": parts[2]})
            elif tag == "PERSONA" and len(parts) >= 4:
                cj.persona.append({"item": parts[1], "confidence": parts[2], "evidence": parts[3]})
            elif tag == "GOAL" and len(parts) >= 4:
                cj.goals.append({"item": parts[1], "confidence": parts[2], "evidence": parts[3]})
            elif tag == "RELATION" and len(parts) >= 4:
                cj.relationships.append({"item": parts[1], "confidence": parts[2], "evidence": parts[3]})
            elif tag == "CAPABILITY" and len(parts) >= 4:
                cj.capabilities.append({"item": parts[1], "confidence": parts[2], "evidence": parts[3]})
            elif tag == "BOUNDARY" and len(parts) >= 3:
                cj.hard_boundaries.append({"item": parts[1], "evidence": parts[2]})
            elif tag == "FACT" and len(parts) >= 3:
                cj.allowed_facts.append({"fact": parts[1], "evidence": parts[2]})
            elif tag == "OPENQ" and len(parts) >= 2:
                cj.open_questions.append(parts[1])
            elif tag == "DONTINFER" and len(parts) >= 2:
                cj.do_not_infer.append(parts[1])
        except Exception:
            # 忽略格式不合格的行，保证“能跑通”
            continue

    return cj


def _character_json_to_compact_markdown(cj: CharacterJSON) -> str:
    """
    把条目结构转成紧凑 markdown，供下一步“填模板”使用。
    说明：比 JSON 更不容易出错，也更便于模型阅读。
    """
    def _fmt_dict_items(items: List[Dict[str, str]], keys: List[str]) -> str:
        out = []
        for it in items:
            parts = []
            for k in keys:
                v = (it.get(k) or "").strip()
                if v:
                    parts.append(f"{k}={v}")
            if parts:
                out.append("- " + "；".join(parts))
        return "\n".join(out) if out else "- （无）"

    def _fmt_str_items(items: List[str]) -> str:
        items2 = [x.strip() for x in items if x and x.strip()]
        return "\n".join(f"- {x}" for x in items2) if items2 else "- （无）"

    return "\n".join(
        [
            "## 抽取条目（供填档案模板使用）",
            f"- character_name: {cj.character_name or ''}",
            "",
            "### VOICE",
            _fmt_dict_items(cj.voice, ["item", "evidence"]),
            "",
            "### PERSONA",
            _fmt_dict_items(cj.persona, ["item", "confidence", "evidence"]),
            "",
            "### GOALS",
            _fmt_dict_items(cj.goals, ["item", "confidence", "evidence"]),
            "",
            "### RELATIONSHIPS",
            _fmt_dict_items(cj.relationships, ["item", "confidence", "evidence"]),
            "",
            "### CAPABILITIES",
            _fmt_dict_items(cj.capabilities, ["item", "confidence", "evidence"]),
            "",
            "### HARD_BOUNDARIES",
            _fmt_dict_items(cj.hard_boundaries, ["item", "evidence"]),
            "",
            "### ALLOWED_FACTS",
            _fmt_dict_items(cj.allowed_facts, ["fact", "evidence"]),
            "",
            "### OPEN_QUESTIONS",
            _fmt_str_items(cj.open_questions),
            "",
            "### DO_NOT_INFER",
            _fmt_str_items(cj.do_not_infer),
        ]
    )


def run_snippet_extractor(
    *,
    novel_path: str,
    character_name: str,
    aliases: List[str],
    output_dir: str,
    context_lines: int = 20,
    require_quote: bool = False,
    exclude_terms: Optional[List[str]] = None,
) -> str:
    os.makedirs(output_dir, exist_ok=True)
    slug = _slugify(character_name)
    out_path = os.path.join(output_dir, f"角色片段-{slug}.md")

    cmd = [
        sys.executable,
        SCRIPTS_EXTRACTOR,
        "--input",
        novel_path,
        "--output",
        out_path,
        "--context-lines",
        str(context_lines),
    ]
    if require_quote:
        cmd.append("--require-quote")
    exclude_terms = exclude_terms or []

    # names
    terms = [character_name] + aliases
    # stable unique
    seen = set()
    uniq_terms = []
    for t in terms:
        t = t.strip()
        if t and t not in seen:
            seen.add(t)
            uniq_terms.append(t)
    for t in uniq_terms:
        cmd.extend(["--name", t])

    for ex in exclude_terms:
        ex = ex.strip()
        if ex:
            cmd.extend(["--exclude", ex])

    # Windows：默认 text 解码依本地代码页，子进程若输出 UTF-8 会触发 UnicodeDecodeError → 上层 500。
    p = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=REPO_ROOT,
    )
    if p.returncode != 0:
        raise RuntimeError((p.stderr or p.stdout or "").strip() or "片段抽取失败。")
    return out_path


def build_character_json_from_snippets(
    *,
    client: DeepSeekClient,
    model: str,
    character_name: str,
    snippets_md_path: str,
    status_cb: Callable[[str], None],
    batch_max_chars: int = 12000,
) -> CharacterJSON:
    md = _read_text(snippets_md_path)
    blocks = _parse_snippets(md)
    if not blocks:
        raise RuntimeError("没有抽取到任何片段。请检查人物名/别名是否正确，或取消“对白优先”。")

    batches = _batch_by_chars(blocks, max_chars=batch_max_chars)
    status_cb(f"片段总数：{len(blocks)}；将分 {len(batches)} 批调用模型归纳。")

    system = (
        "你是严谨的小说人物设定信息抽取器。你必须只依据用户提供的片段做总结，禁止补完未写明设定。"
        "输出必须是“逐行条目格式”，不要输出 JSON、不要输出解释性段落。"
    )

    base = CharacterJSON(character_name=character_name)
    for idx, batch in enumerate(batches, start=1):
        status_cb(f"归纳中：第 {idx}/{len(batches)} 批…")
        user = (
            f"目标人物：{character_name}\n\n"
            "请从以下片段中抽取“可证据化”的设定信息，按如下“逐行条目格式”输出（每行一条，使用 | 分隔）：\n\n"
            "VOICE|<item>|<evidence>\n"
            "PERSONA|<item>|<confidence:高/中/低>|<evidence>\n"
            "GOAL|<item>|<confidence:高/中/低>|<evidence>\n"
            "RELATION|<item>|<confidence:高/中/低>|<evidence>\n"
            "CAPABILITY|<item>|<confidence:高/中/低>|<evidence>\n"
            "BOUNDARY|<item>|<evidence>\n"
            "FACT|<fact>|<evidence>\n"
            "OPENQ|<question>\n"
            "DONTINFER|<item>\n\n"
            "规则（必须遵守）：\n"
            "- evidence 必须是“短摘录”（句子级），不要复制大段章节。\n"
            "- 没有依据就不要写；不确定写 OPENQ 或 DONTINFER。\n"
            "- FACT 只能写文本明确的设定点；不要把推断当 FACT。\n"
            "- 只输出这些条目行，不要写标题、不写总结、不用代码块。\n\n"
            "片段如下：\n"
            f"{batch}\n"
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


def render_character_archive_markdown(
    *,
    client: DeepSeekClient,
    model: str,
    character_json: CharacterJSON,
    source_title: str = "",
) -> str:
    template = _read_text(CHAR_ARCHIVE_TEMPLATE_PATH)
    extracted_md = _character_json_to_compact_markdown(character_json)
    system = (
        "你是严谨的小说人物档案撰写者。你必须仅依据用户提供的“抽取条目”来填写模板，禁止补完未写明设定。"
        "输出必须是完整 markdown，并严格保持模板结构（标题与表头不改名）。"
    )
    user = (
        "请根据以下“抽取条目”，填写下面的《小说人物档案》模板。\n\n"
        "要求：\n"
        "- 每个表格的关键行尽量填写；缺信息可留空，但不要编造。\n"
        "- 证据用短摘录。\n"
        "- 置信度字段只用：高/中/低。\n\n"
        f"抽取条目：\n```markdown\n{extracted_md}\n```\n\n"
        "模板：\n```markdown\n"
        f"{template}\n"
        "```\n"
    )
    resp = client.chat_completions(
        model=model, system=system, user=user, max_tokens=2600, temperature=0.1
    )
    return resp.content.strip()


def render_roleplay_system_prompt_markdown(
    *,
    client: DeepSeekClient,
    model: str,
    character_archive_md: str,
) -> str:
    template = _read_text(ROLEPLAY_SYSTEM_TEMPLATE_PATH)
    system = (
        "你是角色扮演指令编译器。你必须根据用户提供的《人物档案》生成 system prompt，"
        "HardBoundaries 与 DoNotInfer 必须显式写入；AllowedFacts 只能来自档案。"
        "输出必须是完整 markdown，并严格保持模板结构。"
    )
    user = (
        "请把下面的《人物档案（markdown）》编译成《系统提示词（角色扮演稿）》。\n\n"
        "重要规则：\n"
        "- 不要新增出生地/年龄/家人等背景细节。\n"
        "- 不确定时要承认未知并反问用户。\n"
        "- 不复制大段原文；如需例句必须极短且避免原文复刻。\n\n"
        "人物档案：\n```markdown\n"
        f"{character_archive_md}\n"
        "```\n\n"
        "模板：\n```markdown\n"
        f"{template}\n"
        "```\n"
    )
    resp = client.chat_completions(
        model=model, system=system, user=user, max_tokens=2200, temperature=0.1
    )
    return resp.content.strip()


@dataclass
class PipelineResult:
    snippets_path: str
    archive_md: str
    system_prompt_md: str
    archive_path: str
    system_prompt_path: str


def run_full_pipeline(
    *,
    api_key: str,
    novel_path: str,
    character_name: str,
    aliases_text: str,
    output_dir: str = ARTIFACTS_DIR,
    model: str = "deepseek-reasoner",
    status_cb: Callable[[str], None],
) -> PipelineResult:
    status_cb("初始化…")
    os.makedirs(output_dir, exist_ok=True)
    client = DeepSeekClient(api_key=api_key, timeout_s=180, max_retries=4)

    aliases = _split_aliases(aliases_text)

    status_cb("步骤 1/3：全书抽取角色片段…")
    snippets_path = run_snippet_extractor(
        novel_path=novel_path,
        character_name=character_name,
        aliases=aliases,
        output_dir=output_dir,
        context_lines=20,
        require_quote=False,
    )

    status_cb("步骤 2/3：调用模型归纳人物档案（分批）…")
    batch_cap = _env_batch_max_chars()
    cj = build_character_json_from_snippets(
        client=client,
        model=model,
        character_name=character_name,
        snippets_md_path=snippets_path,
        status_cb=status_cb,
        batch_max_chars=batch_cap,
    )

    status_cb("步骤 2/3：渲染人物档案（markdown）…")
    archive_md = render_character_archive_markdown(
        client=client, model=model, character_json=cj
    )

    status_cb("步骤 3/3：生成扮演指令（system prompt）…")
    system_prompt_md = render_roleplay_system_prompt_markdown(
        client=client, model=model, character_archive_md=archive_md
    )

    slug = _slugify(character_name)
    archive_path = os.path.join(output_dir, f"人物档案-{slug}.md")
    system_prompt_path = os.path.join(output_dir, f"扮演指令-{slug}.md")

    with open(archive_path, "w", encoding="utf-8") as f:
        f.write(archive_md + "\n")
    with open(system_prompt_path, "w", encoding="utf-8") as f:
        f.write(system_prompt_md + "\n")

    status_cb("完成。")
    return PipelineResult(
        snippets_path=snippets_path,
        archive_md=archive_md,
        system_prompt_md=system_prompt_md,
        archive_path=archive_path,
        system_prompt_path=system_prompt_path,
    )

