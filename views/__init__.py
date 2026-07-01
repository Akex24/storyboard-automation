# -*- coding: utf-8 -*-
"""
views — главные view-вкладки Storyboard Studio.

Структура:
    views/actors.py — вкладка «Актёры» (ActorsView + ActorCard) [шаг 4C]
    views/camera_lab.py — admin-only прототип Camera Lab
    views/episode_chat.py — чат эпизода (EpisodeChatView + ChatInputEdit) [шаг 5B]
    views/new_episode.py — вкладка «Новый эпизод» (NewEpisodeView) [шаг 5C]

История: вытащено из storyboard_app.py 2026-05-04.
"""

from views.actors import ActorsView, ActorCard
from views.camera_lab import CameraLabView
from views.episode_chat import EpisodeChatView, ChatInputEdit
from views.new_episode import NewEpisodeView

__all__ = [
    'ActorsView',
    'ActorCard',
    'CameraLabView',
    'EpisodeChatView',
    'ChatInputEdit',
    'NewEpisodeView',
]
