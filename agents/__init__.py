# -*- coding: utf-8 -*-
"""
agents/ — системные промпты для multi-agent оркестратора монтажной карты.

Структура:
    agents/montage_prompts.py — три промпта: Сценарист, Чекер, Редактор.
                                Используются в threads/montage_orchestrator.py
                                для последовательных вызовов `claude -p`.

История: создано 2026-05-06 (фича «Multi-agent monton card → storyboards»).
"""

from agents.montage_prompts import (
    SCRIPTWRITER_SYSTEM,
    VALIDATOR_SYSTEM,
    EDITOR_SYSTEM,
    CONTEXT_REVIEWER_SYSTEM,
    build_scriptwriter_user_prompt,
    build_validator_user_prompt,
    build_editor_user_prompt,
    build_context_reviewer_user_prompt,
)
from agents.storyboard_writer_prompts import (
    SYSTEM as STORYBOARD_WRITER_SYSTEM,
    build_user_prompt as build_storyboard_writer_user_prompt,
)

__all__ = [
    'SCRIPTWRITER_SYSTEM',
    'VALIDATOR_SYSTEM',
    'EDITOR_SYSTEM',
    'CONTEXT_REVIEWER_SYSTEM',
    'STORYBOARD_WRITER_SYSTEM',
    'build_scriptwriter_user_prompt',
    'build_validator_user_prompt',
    'build_editor_user_prompt',
    'build_context_reviewer_user_prompt',
    'build_storyboard_writer_user_prompt',
]
