"""
Мониторинг загрузок Steam: чтение content_log.txt, парсинг, формирование отчётов.

Алгоритм:
1. Читаем хвост лога (последние 2 MB) для начального состояния
2. Каждую минуту: читаем новые строки + перечитываем хвост (512 KB) для актуальной скорости
3. Парсим строки: прогресс загрузки, статус (Downloading/Suspended), скорость из лога
4. Формируем отчёт: только активные загрузки или на паузе (название, статус, скорость)
"""

import os
import re
import time
from datetime import datetime

from steam import get_app_name, get_log_path

TAIL_BYTES = 2 * 1024 * 1024
REPORT_INTERVAL_SEC = 60.0
NUM_REPORTS = 5
TAIL_BEFORE_REPORT_BYTES = 512 * 1024

RE_TIMESTAMP = re.compile(r"\[(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})\]\s*(.*)")
RE_DOWNLOAD_START = re.compile(
    r"Starting update AppID\s+(\d+)\s*:\s*download\s+(\d+)\s*/\s*(\d+)", re.IGNORECASE
)
RE_DOWNLOAD_STARTED = re.compile(
    r"AppID\s+(\d+)\s+update started\s*:\s*download\s+(\d+)\s*/\s*(\d+)", re.IGNORECASE
)
RE_STATE = re.compile(r"AppID\s+(\d+)\s+state changed\s*:\s*(.+)", re.IGNORECASE)
RE_APP_UPDATE_CHANGED = re.compile(r"AppID\s+(\d+)\s+App update changed\s*:\s*(.+)", re.IGNORECASE)
RE_CURRENT_RATE = re.compile(r"Current download rate:\s*([\d.]+)\s*Mbps", re.IGNORECASE)
RE_FULLY_INSTALLED = re.compile(r"AppID\s+(\d+)\s+.*Fully Installed", re.IGNORECASE)
RE_FINISHED_UPDATE = re.compile(r"AppID\s+(\d+)\s+finished update", re.IGNORECASE)


def _empty_app():
    return {"downloaded": 0, "total": 0, "paused": False, "downloading": False}


def _read_tail(log_path: str, max_bytes: int = TAIL_BYTES) -> str:
    """Читает последние max_bytes байт лога."""
    if not os.path.isfile(log_path):
        return ""
    size = os.path.getsize(log_path)
    start = max(0, size - max_bytes)
    with open(log_path, "r", encoding="utf-8", errors="replace") as f:
        f.seek(start)
        if start > 0:
            f.readline()
        return f.read()


def _read_from(log_path: str, from_pos: int) -> tuple[str, int]:
    """Читает лог с позиции from_pos до конца. Возвращает (текст, новая_позиция)."""
    if not os.path.isfile(log_path):
        return "", from_pos
    size = os.path.getsize(log_path)
    if from_pos >= size:
        return "", from_pos
    with open(log_path, "r", encoding="utf-8", errors="replace") as f:
        f.seek(from_pos)
        if from_pos > 0:
            f.readline()
        return f.read(), size


def _parse_chunk(text: str) -> tuple[dict[int, dict[str, bool | int]], float | None]:
    """
    Парсит фрагмент лога Steam.
    Возвращает: (словарь app_id -> состояние, последняя ненулевая скорость Mbps или None).
    """
    out = {}
    last_speed_mbps = None
    last_nonzero_speed_mbps = None  # последняя ненулевая скорость (для активных загрузок)
    for line in text.splitlines():
        m = RE_TIMESTAMP.match(line.strip())
        if not m:
            continue
        rest = m.group(2)
        mr = RE_CURRENT_RATE.search(rest)
        if mr:
            speed_val = float(mr.group(1))
            last_speed_mbps = speed_val
            if speed_val > 0:
                last_nonzero_speed_mbps = speed_val
            continue

        # Завершённые загрузки: "Fully Installed" или "finished update"
        mf = RE_FULLY_INSTALLED.search(rest) or RE_FINISHED_UPDATE.search(rest)
        if mf:
            app_id = int(mf.group(1))
            if app_id not in out:
                out[app_id] = _empty_app()
            out[app_id].update(downloading=False, paused=False)
            continue

        matched_dl = False
        for re_dl in (RE_DOWNLOAD_START, RE_DOWNLOAD_STARTED):
            mu = re_dl.search(rest)
            if mu:
                app_id = int(mu.group(1))
                if app_id not in out:
                    out[app_id] = _empty_app()
                out[app_id].update(downloaded=int(mu.group(2)), total=int(mu.group(3)))
                matched_dl = True
                break
        if matched_dl:
            continue

        for re_st in (RE_STATE, RE_APP_UPDATE_CHANGED):
            ms = re_st.search(rest)
            if ms:
                app_id = int(ms.group(1))
                state_str = ms.group(2)
                if app_id not in out:
                    out[app_id] = _empty_app()
                paused = "suspended" in state_str.lower()
                downloading = "Downloading" in state_str and not paused
                # eсли "Fully Installed" в статусе (как в логе стима), то загрузка завершена
                if "Fully Installed" in state_str:
                    downloading = False
                    paused = False
                out[app_id].update(paused=paused, downloading=downloading)
                break
    # Предпочитаем ненулевую скорость (активная загрузка), иначе последнюю (может быть 0)
    return out, last_nonzero_speed_mbps if last_nonzero_speed_mbps is not None else last_speed_mbps


def _merge(state: dict, parsed: dict):
    for app_id, info in parsed.items():
        if app_id not in state:
            state[app_id] = _empty_app()
        state[app_id].update(info)


def _build_report(steam_path: str, state: dict, current_speed_mbps: float | None) -> str:
    """
    Формирует отчёт: игры в загрузке или на паузе.
    Показывает: название, статус (Загрузка/Пауза), скорость из лога Steam.
    """
    # Только активные загрузки (Downloading) или на паузе (Suspended)
    relevant = {
        aid: info for aid, info in state.items()
        if (info.get("downloading") and not info.get("paused")) or info.get("paused")
    }
    if not relevant:
        return "Активных загрузок нет.\n"
    lines = []

    if current_speed_mbps is not None and current_speed_mbps > 0:
        speed_str = f"{current_speed_mbps:.2f} МБит/сек."
    else:
        speed_str = "-"
    for app_id in sorted(relevant.keys()):
        info = relevant[app_id]
        name = get_app_name(steam_path, app_id)
        status = "Пауза" if info.get("paused") else "Загрузка"
        line_speed = "-" if info.get("paused") else speed_str
        lines.append(f"  * {name}")
        lines.append(f"    Статус: {status} | Скорость: {line_speed}")
    return "\n".join(lines) + "\n"


class SteamDownloadMonitor:
    """Мониторинг загрузок Steam: читает content_log.txt, выводит отчёты каждую минуту (по заданию)."""

    def __init__(self, steam_path: str):
        self.steam_path = steam_path
        self.log_path = get_log_path(steam_path)
        self.state = {}  # app_id -> {downloaded, total, paused, downloading}
        self.read_pos = 0  # позиция в логе 
        self.current_speed_mbps: float | None = None  # скорость из лога Steam

    def _load_initial(self) -> None:
        """Первое чтение: хвост лога для начального состояния."""
        text = _read_tail(self.log_path)
        parsed, speed = _parse_chunk(text)
        _merge(self.state, parsed)
        if speed is not None:
            self.current_speed_mbps = speed
        self.read_pos = os.path.getsize(self.log_path) if os.path.isfile(self.log_path) else 0

    def _tick(self) -> tuple[datetime, str]:
        """
        Один цикл мониторинга: ждём REPORT_INTERVAL_SEC, читаем новые строки лога, формируем отчёт.
        """
        time.sleep(REPORT_INTERVAL_SEC)
        now = datetime.now()
        # Читаем новые строки с последней позиции
        text, self.read_pos = _read_from(self.log_path, self.read_pos)
        if text:
            parsed, speed = _parse_chunk(text)
            _merge(self.state, parsed)
            if speed is not None:
                self.current_speed_mbps = speed
        # Перечитываем хвост лога (Steam пишет скорость не каждую минуту)
        tail = _read_tail(self.log_path, max_bytes=TAIL_BEFORE_REPORT_BYTES)
        if tail:
            parsed, speed = _parse_chunk(tail)
            _merge(self.state, parsed)
            if speed is not None:
                self.current_speed_mbps = speed
        return now, _build_report(self.steam_path, self.state, self.current_speed_mbps)

    def run(self, num_reports: int = NUM_REPORTS) -> None:
        """Запуск мониторинга: num_reports отчётов каждую минуту."""
        if not os.path.isfile(self.log_path):
            print(f"Файл лога не найден: {self.log_path}")
            print("Запустите Steam и начните загрузку игры.")
            return
        print("Путь к Steam:", self.steam_path)
        print("Лог:", self.log_path)
        print(f"Мониторинг: {num_reports} отчётов с интервалом 1 минута.\n")

        self._load_initial()
        for i in range(num_reports):
            now, report = self._tick()
            print(f"--- Отчёт {i + 1}/{num_reports} | {now.strftime('%H:%M:%S')} ---")
            print(report)
        print("Мониторинг завершён.")
