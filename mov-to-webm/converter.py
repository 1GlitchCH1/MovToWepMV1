"""
MOV → WebM конвертер с поддержкой альфа-канала
Использует ffmpeg + VP9 кодек (поддерживает прозрачность в WebM)
"""

import tkinter as tk
from tkinter import filedialog, messagebox, scrolledtext
import subprocess
import threading
import re
import os
import sys
import logging
import datetime


# ─── Настройка логирования ────────────────────────────────────────────────────

def setup_logger(log_dir: str) -> logging.Logger:
    os.makedirs(log_dir, exist_ok=True)
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    log_file = os.path.join(log_dir, f"conversion_{timestamp}.log")

    logger = logging.getLogger("MOV2WebM")
    logger.setLevel(logging.DEBUG)

    # Файловый хендлер
    fh = logging.FileHandler(log_file, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    logger.addHandler(fh)

    # Консольный хендлер
    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(logging.DEBUG)
    ch.setFormatter(logging.Formatter("[%(levelname)s] %(message)s"))
    logger.addHandler(ch)

    logger.info(f"Лог-файл: {log_file}")
    return logger


# ─── Вспомогательные функции ──────────────────────────────────────────────────

def find_ffmpeg() -> str | None:
    """Ищет ffmpeg в PATH."""
    try:
        result = subprocess.run(
            ["ffmpeg", "-version"],
            capture_output=True, text=True, timeout=10
        )
        if result.returncode == 0:
            return "ffmpeg"
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    return None


def get_video_duration(input_path: str, logger: logging.Logger) -> float | None:
    """Получает длительность видео в секундах через ffprobe."""
    try:
        cmd = [
            "ffprobe", "-v", "quiet",
            "-print_format", "json",
            "-show_streams",
            input_path
        ]
        logger.debug(f"ffprobe команда: {' '.join(cmd)}")
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if result.returncode != 0:
            logger.warning(f"ffprobe вернул ошибку: {result.stderr}")
            return None

        import json
        data = json.loads(result.stdout)
        for stream in data.get("streams", []):
            if stream.get("codec_type") == "video":
                duration = stream.get("duration")
                if duration:
                    logger.info(f"Длительность видео: {float(duration):.2f} сек")
                    return float(duration)
        logger.warning("Не удалось определить длительность через ffprobe.")
        return None
    except Exception as e:
        logger.warning(f"Ошибка ffprobe: {e}")
        return None


def check_alpha_channel(input_path: str, logger: logging.Logger) -> bool:
    """Проверяет наличие альфа-канала в исходном файле."""
    try:
        cmd = [
            "ffprobe", "-v", "quiet",
            "-print_format", "json",
            "-show_streams",
            input_path
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if result.returncode != 0:
            return False

        import json
        data = json.loads(result.stdout)
        for stream in data.get("streams", []):
            if stream.get("codec_type") == "video":
                pix_fmt = stream.get("pix_fmt", "")
                codec = stream.get("codec_name", "")
                logger.info(f"Видеопоток: кодек={codec}, pix_fmt={pix_fmt}")
                alpha_fmts = [
                    "yuva420p", "yuva422p", "yuva444p",
                    "rgba", "argb", "bgra", "abgr",
                    "rgb24a", "gbrap", "gbrap10le",
                    "yuva420p10le", "yuva422p10le", "yuva444p10le",
                ]
                has_alpha = any(a in pix_fmt for a in ["yuva", "rgba", "bgra", "argb", "abgr", "gbrap"])
                if has_alpha:
                    logger.info(f"✓ Альфа-канал обнаружен (pix_fmt={pix_fmt})")
                else:
                    logger.warning(f"Альфа-канал не обнаружен в метаданных (pix_fmt={pix_fmt}). Продолжаем — файл .mov может содержать прозрачность в кодеке прунас.")
        return True
    except Exception as e:
        logger.warning(f"Не удалось проверить альфа-канал: {e}")
        return True


# ─── Конвертация ──────────────────────────────────────────────────────────────

def convert(
    input_path: str,
    output_path: str,
    logger: logging.Logger,
    progress_callback,
    status_callback,
    done_callback,
    error_callback,
    crf: int = 20,
    threads: int = 0,
):
    """
    Запускает ffmpeg в отдельном потоке.
    VP9 + yuva420p = WebM с альфа-каналом.
    """
    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        error_callback("ffmpeg не найден!\n\nУстановите ffmpeg и добавьте его в PATH.\nhttps://ffmpeg.org/download.html")
        return

    # Проверяем входной файл
    if not os.path.isfile(input_path):
        error_callback(f"Входной файл не существует:\n{input_path}")
        return

    # Создаём папку для выходного файла
    out_dir = os.path.dirname(output_path)
    if out_dir:
        try:
            os.makedirs(out_dir, exist_ok=True)
        except Exception as e:
            error_callback(f"Не удалось создать папку для вывода:\n{out_dir}\n{e}")
            return

    file_size_mb = os.path.getsize(input_path) / (1024 ** 2)
    logger.info(f"=== Начало конвертации ===")
    logger.info(f"Входной файл : {input_path} ({file_size_mb:.1f} МБ)")
    logger.info(f"Выходной файл: {output_path}")
    logger.info(f"CRF          : {crf}")
    logger.info(f"Потоки ffmpeg: {'авто' if threads == 0 else threads}")

    check_alpha_channel(input_path, logger)
    duration = get_video_duration(input_path, logger)

    status_callback("Конвертация...")

    # ──────────────────────────────────────────────────────────────────────────
    # Команда ffmpeg:
    #   -pix_fmt yuva420p   — явно указываем пиксельный формат с альфой
    #   -c:v libvpx-vp9     — VP9 (единственный кодек WebM с альфой)
    #   -auto-alt-ref 0     — ОБЯЗАТЕЛЬНО при альфа-канале, иначе ffmpeg падает
    #   -b:v 0              — режим CRF (без ограничения битрейта)
    #   -crf <crf>          — качество (0=лучшее, 63=худшее)
    #   -an                 — аудио не включаем (у .mov-прозрачностей аудио нет)
    #   -row-mt 1           — многопоточность по строкам (быстрее)
    # ──────────────────────────────────────────────────────────────────────────
    cmd = [
        ffmpeg,
        "-y",                        # перезаписать без вопросов
        "-i", input_path,
        "-c:v", "libvpx-vp9",
        "-pix_fmt", "yuva420p",
        "-auto-alt-ref", "0",
        "-b:v", "0",
        "-crf", str(crf),
        "-row-mt", "1",
        "-an",
        "-progress", "pipe:1",       # прогресс в stdout
        "-nostats",
        "-loglevel", "error",        # только ошибки в stderr
    ]
    if threads > 0:
        cmd += ["-threads", str(threads)]
    cmd.append(output_path)

    logger.info(f"Команда ffmpeg:\n  {' '.join(cmd)}")

    try:
        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )

        current_time_us = 0  # прогресс из ffmpeg в микросекундах
        duration_us = int(duration * 1_000_000) if duration else None

        # Читаем stdout (прогресс-строки ffmpeg)
        for line in process.stdout:
            line = line.strip()
            if line.startswith("out_time_us="):
                try:
                    current_time_us = int(line.split("=")[1])
                    if duration_us and duration_us > 0:
                        pct = min(int(current_time_us / duration_us * 100), 99)
                        progress_callback(pct)
                except ValueError:
                    pass
            elif line.startswith("progress=end"):
                progress_callback(100)

        # Ждём завершения и читаем stderr
        _, stderr = process.communicate()

        if stderr.strip():
            for err_line in stderr.strip().splitlines():
                logger.error(f"ffmpeg stderr: {err_line}")

        if process.returncode != 0:
            logger.error(f"ffmpeg завершился с кодом {process.returncode}")
            error_callback(
                f"ffmpeg завершился с ошибкой (код {process.returncode}).\n"
                f"Подробности см. в лог-файле."
            )
            return

        if not os.path.isfile(output_path):
            logger.error("Выходной файл не создан после конвертации!")
            error_callback("Конвертация завершилась, но выходной файл не найден.\nПроверьте лог-файл.")
            return

        out_size_mb = os.path.getsize(output_path) / (1024 ** 2)
        logger.info(f"=== Конвертация завершена ===")
        logger.info(f"Выходной файл: {output_path} ({out_size_mb:.1f} МБ)")
        done_callback(output_path, out_size_mb)

    except Exception as e:
        logger.exception(f"Неожиданная ошибка при конвертации: {e}")
        error_callback(f"Неожиданная ошибка:\n{e}\n\nСм. лог-файл.")


# ─── UI ───────────────────────────────────────────────────────────────────────

class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("MOV → WebM (Alpha)")
        self.resizable(False, False)
        self.configure(bg="#1e1e2e")

        log_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
        self.logger = setup_logger(log_dir)
        self.logger.info("Приложение запущено")

        self._build_ui()
        self._check_ffmpeg()

    # ── Построение интерфейса ─────────────────────────────────────────────────

    def _build_ui(self):
        PAD = 14
        BG = "#1e1e2e"
        FG = "#cdd6f4"
        ACCENT = "#89b4fa"
        ENTRY_BG = "#313244"
        BTN_BG = "#89b4fa"
        BTN_FG = "#1e1e2e"
        FONT = ("Segoe UI", 10)
        FONT_BOLD = ("Segoe UI", 10, "bold")
        FONT_TITLE = ("Segoe UI", 13, "bold")

        # Заголовок
        tk.Label(
            self, text="MOV → WebM конвертер",
            font=FONT_TITLE, bg=BG, fg=ACCENT
        ).grid(row=0, column=0, columnspan=3, pady=(PAD, 4), padx=PAD)

        tk.Label(
            self, text="Сохраняет альфа-канал (прозрачность) через VP9",
            font=("Segoe UI", 9), bg=BG, fg="#a6adc8"
        ).grid(row=1, column=0, columnspan=3, pady=(0, PAD), padx=PAD)

        # ── Входной файл
        tk.Label(self, text="Входной .mov файл:", font=FONT_BOLD, bg=BG, fg=FG).grid(
            row=2, column=0, sticky="w", padx=PAD, pady=4)

        self.input_var = tk.StringVar()
        self.input_entry = tk.Entry(
            self, textvariable=self.input_var, width=46,
            bg=ENTRY_BG, fg=FG, insertbackground=FG,
            relief="flat", font=FONT
        )
        self.input_entry.grid(row=2, column=1, padx=4, pady=4)

        tk.Button(
            self, text="Обзор...", font=FONT,
            bg="#585b70", fg=FG, activebackground="#6c7086",
            relief="flat", cursor="hand2",
            command=self._browse_input
        ).grid(row=2, column=2, padx=(0, PAD), pady=4)

        # ── Выходной путь
        tk.Label(self, text="Путь к выходному .webm:", font=FONT_BOLD, bg=BG, fg=FG).grid(
            row=3, column=0, sticky="w", padx=PAD, pady=4)

        self.output_var = tk.StringVar()
        self.output_entry = tk.Entry(
            self, textvariable=self.output_var, width=46,
            bg=ENTRY_BG, fg=FG, insertbackground=FG,
            relief="flat", font=FONT
        )
        self.output_entry.grid(row=3, column=1, padx=4, pady=4)

        tk.Button(
            self, text="Обзор...", font=FONT,
            bg="#585b70", fg=FG, activebackground="#6c7086",
            relief="flat", cursor="hand2",
            command=self._browse_output
        ).grid(row=3, column=2, padx=(0, PAD), pady=4)

        # ── CRF
        tk.Label(self, text="Качество (CRF 0–63):", font=FONT_BOLD, bg=BG, fg=FG).grid(
            row=4, column=0, sticky="w", padx=PAD, pady=4)

        crf_frame = tk.Frame(self, bg=BG)
        crf_frame.grid(row=4, column=1, sticky="w", padx=4, pady=4)

        self.crf_var = tk.IntVar(value=20)
        self.crf_slider = tk.Scale(
            crf_frame, from_=0, to=63, orient="horizontal",
            variable=self.crf_var, length=220,
            bg=BG, fg=FG, troughcolor=ENTRY_BG,
            highlightthickness=0, bd=0,
            command=self._update_crf_label
        )
        self.crf_slider.pack(side="left")
        self.crf_label = tk.Label(
            crf_frame, text="20 (хорошее)", width=14,
            font=FONT, bg=BG, fg="#a6e3a1"
        )
        self.crf_label.pack(side="left", padx=6)

        # ── Прогресс-бар
        tk.Label(self, text="Прогресс:", font=FONT_BOLD, bg=BG, fg=FG).grid(
            row=5, column=0, sticky="w", padx=PAD, pady=(12, 4))

        self.progress_canvas = tk.Canvas(
            self, width=460, height=24, bg=ENTRY_BG,
            highlightthickness=0, relief="flat"
        )
        self.progress_canvas.grid(row=5, column=1, columnspan=2, padx=4, pady=(12, 4))
        self._bar = self.progress_canvas.create_rectangle(0, 0, 0, 24, fill=ACCENT, outline="")
        self._pct_text = self.progress_canvas.create_text(
            230, 12, text="0%", fill="#1e1e2e", font=FONT_BOLD
        )

        # ── Статус
        self.status_var = tk.StringVar(value="Готов к работе")
        tk.Label(
            self, textvariable=self.status_var,
            font=FONT, bg=BG, fg="#a6e3a1"
        ).grid(row=6, column=0, columnspan=3, pady=4)

        # ── Кнопка старт
        self.start_btn = tk.Button(
            self, text="▶  Начать конвертацию",
            font=FONT_BOLD, bg=BTN_BG, fg=BTN_FG,
            activebackground="#74c7ec", relief="flat",
            cursor="hand2", padx=18, pady=8,
            command=self._start
        )
        self.start_btn.grid(row=7, column=0, columnspan=3, pady=(8, 4))

        # ── Лог
        tk.Label(self, text="Лог:", font=FONT_BOLD, bg=BG, fg=FG).grid(
            row=8, column=0, sticky="w", padx=PAD, pady=(10, 2))

        self.log_text = scrolledtext.ScrolledText(
            self, width=68, height=12,
            bg="#11111b", fg="#a6adc8",
            insertbackground=FG, font=("Consolas", 9),
            relief="flat", state="disabled"
        )
        self.log_text.grid(row=9, column=0, columnspan=3, padx=PAD, pady=(0, PAD))

        # Перехватываем логи в UI
        self._setup_log_handler()

    def _setup_log_handler(self):
        class UIHandler(logging.Handler):
            def __init__(self_, widget):
                super().__init__()
                self_.widget = widget

            def emit(self_, record):
                msg = self_.format(record)
                def append():
                    self_.widget.configure(state="normal")
                    self_.widget.insert("end", msg + "\n")
                    self_.widget.see("end")
                    self_.widget.configure(state="disabled")
                try:
                    self_.widget.after(0, append)
                except Exception:
                    pass

        handler = UIHandler(self.log_text)
        handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s",
                                               datefmt="%H:%M:%S"))
        self.logger.addHandler(handler)

    # ── Обновление метки CRF ──────────────────────────────────────────────────

    def _update_crf_label(self, val=None):
        v = self.crf_var.get()
        if v <= 15:
            desc, color = "отличное", "#a6e3a1"
        elif v <= 25:
            desc, color = "хорошее", "#a6e3a1"
        elif v <= 35:
            desc, color = "среднее", "#f9e2af"
        else:
            desc, color = "низкое", "#f38ba8"
        self.crf_label.configure(text=f"{v} ({desc})", fg=color)

    # ── Обзор файлов ──────────────────────────────────────────────────────────

    def _browse_input(self):
        path = filedialog.askopenfilename(
            title="Выберите .mov файл",
            filetypes=[("QuickTime Movie", "*.mov"), ("Все файлы", "*.*")]
        )
        if path:
            self.input_var.set(path)
            # Предлагаем выходной путь автоматически
            if not self.output_var.get():
                base, _ = os.path.splitext(path)
                self.output_var.set(base + ".webm")
            self.logger.info(f"Выбран входной файл: {path}")

    def _browse_output(self):
        path = filedialog.asksaveasfilename(
            title="Сохранить как...",
            defaultextension=".webm",
            filetypes=[("WebM Video", "*.webm"), ("Все файлы", "*.*")]
        )
        if path:
            self.output_var.set(path)
            self.logger.info(f"Выбран выходной файл: {path}")

    # ── Проверка ffmpeg ───────────────────────────────────────────────────────

    def _check_ffmpeg(self):
        if find_ffmpeg():
            self.logger.info("ffmpeg найден в системе ✓")
        else:
            self.logger.error("ffmpeg НЕ найден! Установите ffmpeg: https://ffmpeg.org/download.html")
            self.status_var.set("⚠ ffmpeg не найден!")
            messagebox.showwarning(
                "ffmpeg не найден",
                "ffmpeg не найден в PATH.\n\n"
                "Скачайте и установите ffmpeg:\nhttps://ffmpeg.org/download.html\n\n"
                "После установки перезапустите приложение."
            )

    # ── Обновление прогресс-бара ──────────────────────────────────────────────

    def _set_progress(self, pct: int):
        total_w = 460
        fill_w = int(total_w * pct / 100)
        self.progress_canvas.coords(self._bar, 0, 0, fill_w, 24)
        self.progress_canvas.itemconfigure(self._pct_text, text=f"{pct}%")

    def _update_progress(self, pct: int):
        self.after(0, lambda: self._set_progress(pct))

    def _update_status(self, text: str):
        self.after(0, lambda: self.status_var.set(text))

    # ── Колбэки конвертации ───────────────────────────────────────────────────

    def _on_done(self, output_path: str, size_mb: float):
        def _ui():
            self._set_progress(100)
            self.status_var.set("✅  Видео готово!")
            self.start_btn.configure(state="normal")
            messagebox.showinfo(
                "Готово!",
                f"✅  Видео готово!\n\n"
                f"Файл сохранён:\n{output_path}\n"
                f"Размер: {size_mb:.1f} МБ"
            )
        self.after(0, _ui)

    def _on_error(self, message: str):
        def _ui():
            self.status_var.set("❌  Ошибка!")
            self.start_btn.configure(state="normal")
            messagebox.showerror("Ошибка конвертации", message)
        self.after(0, _ui)

    # ── Запуск ────────────────────────────────────────────────────────────────

    def _start(self):
        input_path = self.input_var.get().strip()
        output_path = self.output_var.get().strip()

        if not input_path:
            messagebox.showwarning("Нет файла", "Укажите входной .mov файл.")
            return
        if not output_path:
            messagebox.showwarning("Нет пути", "Укажите путь для выходного .webm файла.")
            return
        if not input_path.lower().endswith(".mov"):
            if not messagebox.askyesno("Предупреждение",
                                       "Входной файл не имеет расширения .mov.\nПродолжить?"):
                return
        if not output_path.lower().endswith(".webm"):
            messagebox.showwarning("Расширение", "Выходной файл должен иметь расширение .webm")
            return

        self.start_btn.configure(state="disabled")
        self._set_progress(0)
        self.status_var.set("Подготовка...")

        threading.Thread(
            target=convert,
            args=(
                input_path,
                output_path,
                self.logger,
                self._update_progress,
                self._update_status,
                self._on_done,
                self._on_error,
            ),
            kwargs={"crf": self.crf_var.get()},
            daemon=True
        ).start()


# ─── Точка входа ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    app = App()
    app.mainloop()
