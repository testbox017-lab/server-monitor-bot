"""Обновления пакетов Ubuntu: список доступных, установка через помощник и отчёт о результате.

Список читаем сами (apt list --upgradable работает без root). Обновить списки пакетов (apt-get update) и
установить обновления может только root, это делает помощник monitor-bot-action: команды apt-refresh и
apt-upgrade security|all. Установка идёт в отдельной службе systemd, результат помощник пишет в RESULT_FILE.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from i18n import _

RESULT_FILE = Path("/run/monitor-bot-apt.json")
LOCK_FILE = Path("/run/monitor-bot-apt.lock")
# Отметка, которую ставит ежедневное обновление списков (apt.daily) и любое apt-get update
STAMPS = (Path("/var/lib/apt/periodic/update-success-stamp"), Path("/var/lib/apt/lists"))
REBOOT_FILE = Path("/var/run/reboot-required")
REBOOT_PKGS = Path("/var/run/reboot-required.pkgs")
MODES = ("security", "all")
LIST_MAX = 40  # столько пакетов показываем в сообщении (Telegram режет длинные)

# «openssl/noble-updates,noble-security 3.0.13-0ubuntu3.5 amd64 [upgradable from: 3.0.13-0ubuntu3.4]»
LINE_RE = re.compile(r"^(\S+?)/(\S+)\s+(\S+)\s+(\S+)(?:\s+\[[^\]:]*:\s*([^\]]+)\])?")


@dataclass
class Pkg:
    name: str
    new: str
    old: str
    suites: str
    held: str = field(default="", compare=False)  # phased — Ubuntu выпускает поэтапно, kept — apt держит

    @property
    def security(self) -> bool:
        return "-security" in self.suites


def parse(text: str) -> list[Pkg]:
    out = []
    for ln in text.splitlines():
        m = LINE_RE.match(ln.strip())
        if m:
            out.append(Pkg(m.group(1), m.group(3), (m.group(5) or "").strip(), m.group(2)))
    # сначала обновления безопасности, внутри — по имени
    return sorted(out, key=lambda p: (not p.security, p.name))


# Заголовки в выводе apt-get upgrade, после которых идут имена пакетов с отступом
HELD_HEADERS = {"The following upgrades have been deferred due to phasing:": "phased",
                "The following packages have been kept back:": "kept"}


def parse_held(text: str) -> dict[str, str]:
    """{пакет: phased|kept} из вывода apt-get upgrade (настоящего или -s)."""
    out: dict[str, str] = {}
    kind = None
    for ln in text.splitlines():
        if ln.strip() in HELD_HEADERS:
            kind = HELD_HEADERS[ln.strip()]
        elif kind and ln[:1] == " ":
            for n in ln.split():
                out.setdefault(n.split(":")[0], kind)
        else:
            kind = None
    return out


def _run(cmd: list[str]) -> subprocess.CompletedProcess | None:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=60, encoding="utf-8",
                              errors="replace", env={**os.environ, "LC_ALL": "C"})
    except Exception:  # noqa: BLE001
        return None


def held() -> dict[str, str]:
    """Что apt-get upgrade сейчас не поставит. Симуляция (-s) работает без root."""
    r = _run(["apt-get", "-s", "-q", "upgrade"]) if shutil.which("apt-get") else None
    return parse_held(r.stdout) if r and r.returncode == 0 else {}


def upgradable() -> list[Pkg] | None:
    """Доступные обновления. None — apt недоступен (не Debian/Ubuntu) или не ответил."""
    if not shutil.which("apt"):
        return None
    r = _run(["apt", "list", "--upgradable"])
    if r is None or r.returncode != 0:
        return None
    pkgs = parse(r.stdout)
    if pkgs:
        h = held()
        for p in pkgs:
            p.held = h.get(p.name, "")
    return pkgs


def installable(pkgs: list[Pkg], mode: str) -> list[Pkg]:
    """Что реально поставит установка: для «всех» — без отложенных Ubuntu и удержанных apt."""
    return [p for p in pkgs if p.security] if mode == "security" else [p for p in pkgs if not p.held]


def lists_age() -> float | None:
    """Когда последний раз обновлялись списки пакетов (unix-время), None — неизвестно."""
    for p in STAMPS:
        try:
            return p.stat().st_mtime
        except OSError:
            continue
    return None


def running(lock: Path | None = None) -> bool:
    return (lock or LOCK_FILE).exists()


def reboot_info() -> list[str] | None:
    """None — перезагрузка не нужна, иначе пакеты, которые её требуют (может быть пустым)."""
    if not REBOOT_FILE.exists():
        return None
    try:
        return sorted({ln.strip() for ln in REBOOT_PKGS.read_text(encoding="utf-8").splitlines() if ln.strip()})
    except OSError:
        return []


def last_result(path: Path | None = None) -> dict | None:
    try:
        data = json.loads((path or RESULT_FILE).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def pkg_line(p: Pkg) -> str:
    old = f"{p.old} → " if p.old else ""
    return f"{'🛡' if p.security else '⏸' if p.held else '•'} {p.name} {old}{p.new}"


def view_lines(pkgs: list[Pkg] | None) -> list[str]:
    """Текст списка (без HTML-разметки, экранирует вызывающий)."""
    if pkgs is None:
        return [_('Не удалось получить список: apt недоступен или не ответил')]
    if not pkgs:
        return [_('✅ Все пакеты обновлены')]
    sec = sum(p.security for p in pkgs)
    lines = [_('Доступно обновлений: {len}, из них безопасности: {sec}', len=len(pkgs), sec=sec)]
    lines += [pkg_line(p) for p in pkgs[:LIST_MAX]]
    if len(pkgs) > LIST_MAX:
        lines.append(_('…и ещё {n}', n=len(pkgs) - LIST_MAX))
    if any(p.held == "phased" for p in pkgs):
        lines.append(_('⏸ — Ubuntu выпускает это обновление поэтапно, до этого сервера очередь ещё не дошла. '
                       'Когда дойдёт (обычно за несколько дней), его поставит «Установить все».'))
    if any(p.held == "kept" for p in pkgs):
        lines.append(_('⏸ — apt держит обновление: ему нужны новые пакеты (часто так бывает с ядром). Ставится '
                       'вручную: apt full-upgrade.'))
    return lines


def names_short(names: list[str], n: int = 8) -> str:
    return ", ".join(names[:n]) + (_(' и др.') if len(names) > n else "")


def mode_text(mode: str | None) -> str:
    return _('обновления безопасности') if mode == "security" else _('все обновления')


def result_text(res: dict) -> str:
    """Отчёт об установке по файлу результата помощника."""
    what = mode_text(res.get("mode"))
    done = res.get("upgraded") or []
    lines = []
    if res.get("ok"):
        if done:
            lines.append(_('✅ Установлены {what}: {len} пак. ({names})',
                           what=what, len=len(done), names=names_short(done)))
        else:
            lines.append(_('✅ Установка ({what}) завершена, новых версий не было', what=what))
    else:
        lines.append(_('❌ Установка ({what}) не удалась: {error}', what=what, error=res.get("error") or "?"))
        if done:
            lines.append(_('Успели обновиться: {len} пак. ({names})', len=len(done), names=names_short(done)))
    phased = set(res.get("phased") or [])
    left = [n for n in res.get("left") or [] if n not in phased]
    later = [n for n in res.get("left") or [] if n in phased]
    if later:
        lines.append(_('⏸ Отложены Ubuntu (поэтапный выпуск, придут позже): {len} ({names})',
                       len=len(later), names=names_short(later)))
    if left:
        lines.append(_('Остались без обновления: {len} ({names})', len=len(left), names=names_short(left)))
    if res.get("restarted"):
        lines.append(_('Перезапущены службы: {names}', names=names_short(res["restarted"])))
    if res.get("reboot_required"):
        pk = res.get("reboot_pkgs") or []
        lines.append(_('♻️ Нужна перезагрузка сервера') + (f" ({names_short(pk)})" if pk else ""))
    if not res.get("ok") and res.get("log_tail"):
        lines.append(_('Последние строки:\n{tail}', tail=res["log_tail"][-800:]))
    return "\n".join(lines)
