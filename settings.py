"""Настройки бота: config.yaml (токен, доступ, значения по умолчанию) + settings.json (то, что меняется из Telegram)."""
from __future__ import annotations

import copy
import json
import os
import shutil
import sys
from pathlib import Path

import yaml
import i18n
from i18n import _

# Ключи, которые можно менять из Telegram. Всё остальное (token, allowed_chats) только в config.yaml.
EDITABLE = ("thresholds", "check_interval_sec", "services", "containers", "processes", "disks",
            "quiet_hours", "remind_min", "daily_summary", "tz", "notify_on_start",
            "containers_auto", "log_alerts", "log_alert_min", "log_patterns",
            "ssh_alerts", "ssh_alert_all_logins", "ssh_fail_threshold", "port_alerts", "update_alerts", "update_alerts_all",
            "autoban", "autoban_threshold", "autoban_hours", "autoban_whitelist",
            "autoban_hour_threshold", "autoban_net_ips", "autoban_repeat_forever", "log_check_min",
            "probes", "probe_fail_count", "ssl_alerts", "ssl_warn_days",
            "anomaly_alerts", "ram_trend_alerts", "group_alerts",
            "backup_time", "backup_keep", "backup_send_daily", "backup_alerts",
            "offsite_max_age_h", "verify_backups", "weekly_report", "lang", "server_name")

DEFAULTS = {
    "check_interval_sec": 60,
    "notify_on_start": True,
    "thresholds": {"cpu": 90, "ram": 90, "swap": 80, "disk": 85, "temp": 85, "inode": 90,
                   "duration_sec": 300},
    "disks": [],
    "services": [],
    "processes": [],
    "containers": [],
    "quiet_hours": None,        # {"start": "23:00", "end": "08:00"}
    "remind_min": 60,           # 0 = не напоминать
    "daily_summary": None,      # "09:00"
    "weekly_report": "09:00",   # недельный отчёт по понедельникам (None = выкл)
    "tz": "Europe/Kyiv",
    "lang": "ru",               # язык интерфейса: ru, uk, en (меняется в «Настройки → Язык» или /lang)
    "containers_auto": False,   # отслеживать все контейнеры, которые бот видел работающими
    "log_alerts": True,         # алерт на ошибки в логах контейнеров
    "log_alert_min": 15,        # не чаще раза в N минут на контейнер
    "log_check_min": 5,         # просматривать логи раз в N минут (каждый просмотр — вызов через sudo)
    "log_patterns": None,       # None = Traceback, ERROR, CRITICAL, Exception, FATAL
    # Безопасность
    "ssh_alerts": True,             # вход по SSH с нового IP и подбор пароля
    "ssh_alert_all_logins": False,  # сообщать о каждом входе, а не только с нового IP
    "ssh_fail_threshold": 30,       # неудачных попыток за 10 мин для алерта (0 = выкл)
    "port_alerts": True,            # новый открытый наружу порт
    "update_alerts": True,          # обновления безопасности Ubuntu
    "update_alerts_all": False,     # сообщать и об обычных обновлениях (не только безопасности)
    "autoban": True,                # сам блокировать IP, который подбирает пароль SSH
    "autoban_threshold": 10,        # неудачных попыток с одного IP за 10 мин
    "autoban_hours": 24,            # на сколько (0 = навсегда)
    "autoban_whitelist": [],        # эти IP не блокировать никогда (и IP, с которых уже входили)
    "autoban_hour_threshold": 20,   # или столько попыток с одного IP за час (медленный подбор), 0 = выкл
    "autoban_net_ips": 2,           # столько подборщиков из одной /24 — блокировать всю подсеть (0 = выкл)
    "autoban_repeat_forever": True, # кого уже банили за 30 дней, второй раз — навсегда
    # Автодеплой (только config.yaml): вход этим ключом или с этих адресов — не «новый IP», а деплой,
    # и перезапуск контейнера в следующие 10 минут не тревога, а запись в журнале
    "deploy_keys": [],              # отпечатки ключей: SHA256:… (видно в journalctl -u ssh: Accepted publickey …)
    "deploy_nets": [],              # подсети, например 20.0.0.0/8 (лучше ключ: у GitHub Actions адреса разные)
    # Умные алерты
    "anomaly_alerts": True,         # «необычно для этого часа» по истории за 14 дней
    "ram_trend_alerts": True,       # память стабильно растёт и скоро закончится
    "group_alerts": True,           # больше 3 алертов за раз — одним сообщением
    # Резервные копии
    "backup_time": "03:45",         # копия самого бота каждый день (None = выкл)
    "backup_keep": 14,              # сколько копий бота хранить
    "backup_send_daily": False,     # присылать копии в Telegram (копия бота содержит токен!)
    "backup_alerts": True,          # алерт, если бэкап устарел или подозрительно маленький
    "offsite_max_age_h": 72,        # алерт, если ПК не забирал копии дольше (None = выкл)
    "verify_backups": True,         # по воскресеньям в 04:30 проверять, что копии читаются целиком
    # Проверки снаружи
    "probes": [],                   # https://site.ru, https://site.ru/health | OK, tcp://host:5432, ping://host, dns://name
    "probe_timeout": 10,            # секунд на одну проверку
    "probe_fail_count": 2,          # сколько неудач подряд считать падением
    "ssl_alerts": True,             # предупреждать об окончании SSL-сертификатов
    "ssl_warn_days": 14,            # за сколько дней начинать предупреждать
    "heartbeat_url": None,          # URL для пинга раз в проверку (healthchecks.io и т.п.), только config.yaml
    # Действия из Telegram (только config.yaml, кнопками не меняются)
    "actions_enabled": True,
    "action_services": [],      # какие службы можно перезапускать; должны совпадать с /etc/monitor-bot/allowed-services
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
            sys.exit(_('Укажите token в {config_path}', config_path=config_path))
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
        i18n.set_lang(cfg.get("lang"))

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

    def unset(self, key: str) -> None:
        """Убрать изменение из Telegram: снова действует значение из config.yaml."""
        if self.overrides.pop(key, None) is not None:
            self._save()
        self._merge()

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
