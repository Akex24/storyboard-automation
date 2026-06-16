# -*- coding: utf-8 -*-
"""
widgets — переиспользуемые UI-компоненты Storyboard Studio.

Структура:
    widgets/dialogs.py — независимые диалоги (FullscreenImage, RefDoneNotice,
                         GeometryDoneNotice, CloseConfirm)
    widgets/actor_dialogs.py — диалоги привязанные к актёрам (TODO шаг 4)

История: вытащено из storyboard_app.py 2026-05-04 (шаг 3 рефакторинга).
"""

from widgets.dialogs import (
    FullscreenImageDialog,
    RefDoneNoticeDialog,
    GeometryDoneNoticeDialog,
    CloseConfirmDialog,
)
from widgets.actor_dialogs import (
    AddActorDialog,
    ChooseActorDialog,
    ActorPhotosDialog,
    CreateActorRefDialog,
    RefResultDialog,
)
from widgets.editor_widgets import (
    OverlayActionBtn,
    ShotCard,
    RoundedTopImage,
    RefCard,
)
from widgets.gen_button import GenButton
from widgets.ref_picker_dialog import RefPickerDialog
from widgets.character_outfit_picker import CharacterOutfitPicker
from widgets.prompt_retry_dialog import PromptRetryDialog
from widgets.auth_banner import AuthBanner
from widgets.montage_cta import MontageCTA
from widgets.montage_summary_dialog import MontageSummaryDialog
from widgets.active_gens_panel import ActiveGensPanel
from widgets.shot_viewer_dialog import ShotViewerDialog
from widgets.provider_toggle import ProviderToggle

__all__ = [
    # dialogs.py
    'FullscreenImageDialog',
    'RefDoneNoticeDialog',
    'GeometryDoneNoticeDialog',
    'CloseConfirmDialog',
    # actor_dialogs.py (4A + 4B)
    'AddActorDialog',
    'ChooseActorDialog',
    'ActorPhotosDialog',
    'CreateActorRefDialog',
    'RefResultDialog',
    # editor_widgets.py (5A)
    'OverlayActionBtn',
    'ShotCard',
    'RoundedTopImage',
    'RefCard',
    # gen_button.py (sub-MVP «кнопка автономной генерации в чате»)
    'GenButton',
    # ref_picker_dialog.py (Phase 2 hotfix #24 — попап с превью рефов)
    'RefPickerDialog',
    # character_outfit_picker.py (Долг 13 — 3 варианта одежды для character)
    'CharacterOutfitPicker',
    # prompt_retry_dialog.py (2026-05-05 — AI-смягчение promp'а после ошибки)
    'PromptRetryDialog',
    # auth_banner.py (2026-05-06 — плашка смены AI-аккаунта)
    'AuthBanner',
    # montage_cta.py / montage_summary_dialog.py (2026-05-06 — multi-agent монтаж)
    'MontageCTA',
    'MontageSummaryDialog',
    # active_gens_panel.py (2026-05-07 — попап параллельных генераций)
    'ActiveGensPanel',
    # shot_viewer_dialog.py (2026-05-07 — попап просмотра шота с версиями)
    'ShotViewerDialog',
    # provider_toggle.py (2026-06-16 — сегмент-контрол провайдера в Настройках)
    'ProviderToggle',
]
