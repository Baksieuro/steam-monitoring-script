"""Поиск Steam (реестр/стандартные пути), путь к логу, название игры по AppID (все библиотеки)."""

import os
import re

try:
    import winreg
except ImportError:
    winreg = None

# Ключи реестра Windows (путь, имя значения)
_REGISTRY_KEYS = [
    (r"SOFTWARE\WOW6432Node\Valve\Steam", "InstallPath"),
    (r"SOFTWARE\Valve\Steam", "InstallPath"),
    (r"Software\Valve\Steam", "SteamPath"),
]
_REGISTRY_ROOTS = None
if winreg:
    _REGISTRY_ROOTS = (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER)

_FALLBACK_PATHS = [
    os.path.expandvars(r"%ProgramFiles(x86)%\Steam"),
    os.path.expandvars(r"%ProgramFiles%\Steam"),
    os.path.expanduser(r"~\Steam"),
]


def get_steam_path():
    """Находит путь к Steam через реестр Windows или стандартные пути."""
    if winreg and _REGISTRY_ROOTS:
        for key_path, value_name in _REGISTRY_KEYS:
            for root in _REGISTRY_ROOTS:
                try:
                    key = winreg.OpenKey(root, key_path)
                    path = winreg.QueryValueEx(key, value_name)[0]
                    winreg.CloseKey(key)
                    if path and os.path.isdir(path):
                        return os.path.normpath(path)
                except (OSError, FileNotFoundError):
                    pass
    for path in _FALLBACK_PATHS:
        if path and os.path.isdir(path):
            return os.path.normpath(path)
    return None


def get_log_path(steam_path):
    """Путь к content_log.txt."""
    return os.path.join(steam_path, "logs", "content_log.txt")


def _get_library_paths(steam_path):
    """Список всех библиотек Steam (основная + из libraryfolders.vdf)."""
    paths = [steam_path]
    vdf = os.path.join(steam_path, "steamapps", "libraryfolders.vdf")
    if not os.path.isfile(vdf):
        return paths
    try:
        with open(vdf, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
        for m in re.finditer(r'"path"\s+"([^"]+)"', content):
            path = m.group(1).replace("\\\\", "\\").strip()
            if path and os.path.isdir(path) and path not in paths:
                paths.append(path)
    except OSError:
        pass
    return paths


def get_app_name(steam_path, app_id):
    """Название игры по AppID (ищет во всех библиотеках Steam)."""
    for lib_path in _get_library_paths(steam_path):
        acf = os.path.join(lib_path, "steamapps", f"appmanifest_{app_id}.acf")
        if not os.path.isfile(acf):
            continue
        try:
            with open(acf, "r", encoding="utf-8", errors="replace") as f:
                m = re.search(r'"name"\s+"([^"]*)"', f.read())
            if m and m.group(1).strip():
                return m.group(1).strip()
        except OSError:
            continue
    return f"AppID {app_id}"
