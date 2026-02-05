"""Точка входа: мониторинг загрузок Steam (мониторинг загрузок по заданному интервалу)."""

from steam import get_steam_path
from monitor import SteamDownloadMonitor


def main():
    steam_path = get_steam_path()
    if not steam_path:
        print("Не удалось найти папку Steam (реестр и стандартные пути).")
        return
    SteamDownloadMonitor(steam_path).run()


if __name__ == "__main__":
    main()
