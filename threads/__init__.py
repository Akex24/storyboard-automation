# -*- coding: utf-8 -*-
"""
threads — фоновые QThread-классы Storyboard Studio.

Структура:
    threads/update.py   — обновления (CheckUpdate, DownloadUpdate,
                          DownloadAppUpdate, SendUpdate, FetchStats)
    threads/generate.py — генерация (Generate, RefGenerate,
                          GenerateActorRef, ClaudeGeometry, RunEpisode)

История: вытащено из storyboard_app.py 2026-05-04.
"""

from threads.update import (
    CheckUpdateThread,
    DownloadUpdateThread,
    DownloadAppUpdateThread,
    SendUpdateThread,
    FetchStatsThread,
)
from threads.generate import (
    GenerateThread,
    RefGenerateThread,
    GenerateActorRefThread,
    EditActorRefThread,
    ApplyTextureThread,
    ClaudeGeometryThread,
    RunEpisodeThread,
)
from threads.autonomous_gen import AutonomousGenThread
from threads.suggest_outfits import SuggestOutfitsThread
from threads.soften_prompt import SoftenPromptThread
from threads.auth_switch import AuthSwitchThread
from threads.montage_orchestrator import MontageOrchestratorThread
from threads.storyboard_pipeline import StoryboardPipelineThread

__all__ = [
    # update
    'CheckUpdateThread',
    'DownloadUpdateThread',
    'DownloadAppUpdateThread',
    'SendUpdateThread',
    'FetchStatsThread',
    # generate
    'GenerateThread',
    'RefGenerateThread',
    'GenerateActorRefThread',
    'EditActorRefThread',
    'ApplyTextureThread',
    'ClaudeGeometryThread',
    'RunEpisodeThread',
    # autonomous_gen (sub-MVP «кнопка автономной генерации в чате»)
    'AutonomousGenThread',
    # suggest_outfits (Долг 13 — 3 варианта одежды для character)
    'SuggestOutfitsThread',
    # soften_prompt (2026-05-05 — AI-смягчение отклонённого описания)
    'SoftenPromptThread',
    # auth_switch (2026-05-06 — смена AI-аккаунта из Studio)
    'AuthSwitchThread',
    # montage_orchestrator (2026-05-06 — multi-agent монтажная карта)
    'MontageOrchestratorThread',
    # storyboard_pipeline (2026-05-06 — Этап 2: PromptWriter из карты)
    'StoryboardPipelineThread',
]
