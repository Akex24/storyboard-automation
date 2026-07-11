# -*- coding: utf-8 -*-
"""video/mediacheck.py — хедлес-проверка ВОСПРОИЗВЕДЕНИЯ видео (Qt multimedia backend
реально декодит кадры в СОБРАННОМ .app/.exe, а не чёрный экран у коллег).

Как --wm-selftest для ffmpeg: запускается из собранного бинаря
    "…/Storyboard Studio" --video-selftest <clip.mp4>
создаёт QMediaPlayer + QVideoSink, проигрывает клип, СЧИТАЕТ реальные кадры
(videoFrameChanged), проверяет что кадр не чёрный и есть аудиодорожка. Frames>0 →
media-backend (plugins/multimedia/*) присутствует и работает.

Только Qt (QtMultimedia/QtGui/QtWidgets) — без subprocess/shell, cross-platform.
"""
import sys


def video_playback_selftest(path) -> int:
    from PyQt6.QtWidgets import QApplication
    from PyQt6.QtMultimedia import QMediaPlayer, QAudioOutput, QVideoSink
    from PyQt6.QtCore import QUrl, QTimer

    app = QApplication.instance() or QApplication(sys.argv)
    st = {"frames": 0, "w": 0, "h": 0, "nonblack": False, "audio": 0, "err": ""}

    sink = QVideoSink()

    def on_frame(f):
        st["frames"] += 1
        try:
            img = f.toImage()
            if img is not None and not img.isNull():
                st["w"], st["h"] = img.width(), img.height()
                if not st["nonblack"] and img.width() > 2 and img.height() > 2:
                    cx, cy = img.width() // 2, img.height() // 2
                    for sx, sy in ((cx, cy), (cx // 2, cy // 2), (cx + cx // 2, cy + cy // 2)):
                        c = img.pixelColor(int(sx), int(sy))
                        if c.red() + c.green() + c.blue() > 24:   # не полностью чёрный
                            st["nonblack"] = True
                            break
        except Exception:
            pass

    sink.videoFrameChanged.connect(on_frame)

    player = QMediaPlayer()
    audio = QAudioOutput()
    player.setAudioOutput(audio)
    player.setVideoSink(sink)

    def on_err(e, msg):
        st["err"] = f"{e} {msg}"
    try:
        player.errorOccurred.connect(on_err)
    except Exception:
        pass

    def on_tracks():
        try:
            st["audio"] = len(player.audioTracks())
        except Exception:
            pass
    try:
        player.tracksChanged.connect(on_tracks)
    except Exception:
        pass

    from pathlib import Path
    player.setSource(QUrl.fromLocalFile(str(Path(path).expanduser())))
    player.play()
    QTimer.singleShot(3500, app.quit)
    app.exec()

    pos = 0
    try:
        pos = int(player.position())
    except Exception:
        pass
    ok = st["frames"] > 0 and st["nonblack"]
    print(f"[video-selftest] frames={st['frames']} size={st['w']}x{st['h']} "
          f"nonblack={st['nonblack']} audio_tracks={st['audio']} pos={pos}ms err='{st['err']}'")
    print(f"[video-selftest] RESULT={'PLAYS (backend ok, кадры декодятся)' if ok else 'FAIL (чёрный/нет кадров)'}")
    return 0 if ok else 1
