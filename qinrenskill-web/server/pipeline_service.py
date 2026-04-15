"""
在临时目录中跑完整生成流水线，避免落盘敏感文件到 artifacts。
"""

from __future__ import annotations

import os
import tempfile
from typing import Callable

# 保证可导入顶层 app
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.pipeline import run_full_pipeline


def generate_hidden_system_prompt(
    *,
    api_key: str,
    novel_path: str,
    character_name: str,
    aliases_text: str,
    chat_rules: str,
    status_cb: Callable[[str], None],
    model: str | None = None,
) -> str:
    resolved_model = (model or "").strip() or os.environ.get(
        "PIPELINE_MODEL", "deepseek-reasoner"
    )
    with tempfile.TemporaryDirectory(prefix="qinrenskill_") as tmp:
        result = run_full_pipeline(
            api_key=api_key,
            novel_path=novel_path,
            character_name=character_name,
            aliases_text=aliases_text,
            output_dir=tmp,
            model=resolved_model,
            status_cb=status_cb,
        )
        base = result.system_prompt_md.strip()
        rules = (chat_rules or "").strip()
        if rules:
            return base + "\n\n## 对话补充规则（用户指定）\n\n" + rules
        return base
