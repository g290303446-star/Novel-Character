"""
服务端预设扮演指令（快速体验）；全文仅存 data/pavel_preset.txt，仅在建会话时使用。
"""

from __future__ import annotations

from pathlib import Path

_DIR = Path(__file__).resolve().parent
_DATA = _DIR / "data" / "pavel_preset.txt"

PAVEL_PRESET_SYSTEM = _DATA.read_text(encoding="utf-8")

PRESET_PAVEL = "pavel"


def resolve_preset(preset: str) -> tuple[str, str]:
    """返回 (hidden_system_prompt, display_character_name)。"""
    p = (preset or "").strip().lower()
    if p == PRESET_PAVEL:
        return PAVEL_PRESET_SYSTEM, "保尔·柯察金"
    raise ValueError(f"未知预设：{preset!r}")
