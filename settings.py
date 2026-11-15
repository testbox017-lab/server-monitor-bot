"""Настройки бота: config.yaml (токен, доступ, значения по умолчанию) + settings.json (то, что меняется из Telegram)."""
from __future__ import annotations

import copy
import json
import os
import shutil
import sys
from pathlib import Path

import yaml

# Ключи, которые можно менять из Telegram. Всё остальное (token, allowed_chats) только в config.yaml.
EDITABLE = ("thresholds", "check_interval_sec", "services", "containers", "processes", "disks",
            "quiet_hours", "remind_min", "daily_summary", "tz", "notify_on_start")

DEFAULTS = {
    "check_interval_sec": 60,
    "notify_on_start": True,
    "thresholds": {"cpu": 90, "ram": 90, "swap": 80, "disk": 85, "temp": 85, "duration_sec": 300},
    "disks": [],
    "services": [],
    "processes": [],
    "containers": [],
    "quiet_hours": None,        # {"start": "23:00", "end": "08:00"}
    "remind_min": 60,           # 0 = не напоминать
    "daily_summary": None,      # "09:00"
    "tz": "Europe/Kyiv",
}


def data_dir(cfg: dict, config_path: Path) -> Path:
    """Куда бот пишет settings.json и state.json. systemd StateDirectory= передаёт STATE_DIRECTORY."""
    d = cfg.get("data_dir") or os.environ.get("STATE_DIRECTORY") or config_path.parent
    p = Path(str(d).split(":")[0])
    p.mkdir(parents=True, exist_ok=True)
    return p


class Settings:
    def __init__(self, config_path: Path):
        self.config_path = config_path
        with open(config_path, encoding="utf-8") as f:
            base = yaml.safe_load(f) or {}
        token = str(base.get("token") or "")
        if not token or "ВАШ" in token:
            sys.exit(f"Укажите token в {config_path}")
        self.base = copy.deepcopy(DEFAULTS)
        for k, v in base.items():
            if k == "thresholds":
                self.base["thresholds"].update(v or {})
            else:
                self.base[k] = v
        self.base["allowed_chats"] = [int(x) for x in (base.get("allowed_chats") or [])]
        self.dir = data_dir(self.base, config_path)
        self.path = self.dir / "settings.json"
        self.overrides: dict = {}
        if self.path.exists():
            try:
                self.overrides = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                self.overrides = {}
        self.cfg: dict = {}
        self._merge()

    def _merge(self) -> None:
        cfg = copy.deepcopy(self.base)
        for k, v in self.overrides.items():
            if k not in EDITABLE:
                continue
            if k == "thresholds":
                cfg["thresholds"].update(v or {})
            else:
                cfg[k] = v
        self.cfg.clear()
        self.cfg.update(cfg)

    def set(self, key: str, value) -> None:
        """Изменить настройку и сохранить. Старый файл уходит в settings.json.bak."""
        if key.startswith("thresholds."):
            th = dict(self.overrides.get("thresholds") or {})
            th[key.split(".", 1)[1]] = value
            self.overrides["thresholds"] = th
        elif key in EDITABLE:
            self.overrides[key] = value
        else:
            raise KeyError(key)
        self._save()
        self._merge()

    def toggle_item(self, key: str, name: str) -> bool:
        """Добавить/убрать элемент списка (services/containers/processes). True = теперь в списке."""
        items = list(self.cfg.get(key) or [])
        if name in items:
            items.remove(name)
            on = False
        else:
            items.append(name)
            on = True
        self.set(key, items)
        return on

    def reset(self) -> None:
        self.overrides = {}
        self._save()
        self._merge()

    def _save(self) -> None:
        if self.path.exists():
            shutil.copy2(self.path, self.path.with_name("settings.json.bak"))
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.overrides, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)
