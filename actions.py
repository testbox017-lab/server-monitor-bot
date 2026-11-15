"""Действия над сервером из Telegram. Всё, что требует root, идёт через deploy/monitor-bot-action и sudo."""
from __future__ import annotations

import secrets
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

import psutil

HELPER = "/usr/local/sbin/monitor-bot-action"
CONFIRM_TTL = 120  # секунд на подтверждение


@dataclass
class Pending:
    kind: str                 # ctr / svc / kill / clean / reboot
    arg: str = ""
    chat_id: int = 0
    expires: float = 0.0
    stage: int = 1            # для перезагрузки нужно два подтверждения
    extra: dict = field(default_factory=dict)


class Actions:
    def __init__(self, log_path: Path, helper: str = HELPER):
        self.log_path = log_path
        self.helper = helper
        self.pending: dict[str, Pending] = {}

    # ---------- подтверждения ----------

    def ask(self, kind: str, arg: str, chat_id: int, **extra) -> str:
        now = time.time()
        self.pending = {k: p for k, p in self.pending.items() if p.expires > now}
        token = secrets.token_hex(4)
        self.pending[token] = Pending(kind, arg, chat_id, now + CONFIRM_TTL, 1, extra)
        return token

    def take(self, token: str, chat_id: int) -> Pending | None:
        p = self.pending.get(token)
        if not p or p.chat_id != chat_id:
            return None  # чужое подтверждение не трогаем
        if p.expires < time.time():
            self.pending.pop(token, None)
            return None
        return p

    def drop(self, token: str) -> None:
        self.pending.pop(token, None)

    # ---------- выполнение ----------

    def _helper(self, *args: str, timeout: int = 300) -> tuple[bool, str]:
        try:
            r = subprocess.run(["sudo", "-n", self.helper, *args], capture_output=True, text=True,
                               timeout=timeout)
        except Exception as e:  # noqa: BLE001
            return False, str(e)
        out = (r.stdout + r.stderr).strip()
        if "a password is required" in out or "not allowed" in out:
            out = "нет прав: на сервере не настроено правило sudo для бота (см. README)"
        return r.returncode == 0, out

    def restart_container(self, name: str) -> tuple[bool, str]:
        try:
            r = subprocess.run(["docker", "restart", name], capture_output=True, text=True, timeout=120)
        except Exception as e:  # noqa: BLE001
            return False, str(e)
        return r.returncode == 0, (r.stdout + r.stderr).strip()

    def restart_service(self, name: str) -> tuple[bool, str]:
        return self._helper("restart-service", name, timeout=150)

    def kill(self, pid: int, name: str, created: float) -> tuple[bool, str]:
        try:
            p = psutil.Process(pid)
            if p.name() != name or abs(p.create_time() - created) > 1:
                return False, "процесс уже завершился (PID занят другим процессом)"
        except psutil.NoSuchProcess:
            return False, "процесс уже завершился"
        return self._helper("kill", str(pid), timeout=15)

    def cleanup_estimate(self) -> str:
        ok, out = self._helper("cleanup-estimate", timeout=60)
        return out if ok else f"оценить не удалось: {out}"

    def cleanup(self) -> tuple[bool, str]:
        return self._helper("cleanup", timeout=600)

    def reboot(self) -> tuple[bool, str]:
        return self._helper("reboot", timeout=15)

    def execute(self, p: Pending) -> tuple[bool, str]:
        if p.kind == "ctr":
            res = self.restart_container(p.arg)
        elif p.kind == "svc":
            res = self.restart_service(p.arg)
        elif p.kind == "kill":
            res = self.kill(int(p.arg), p.extra.get("name", ""), p.extra.get("created", 0))
        elif p.kind == "clean":
            res = self.cleanup()
        elif p.kind == "reboot":
            res = self.reboot()
        else:
            res = (False, "неизвестное действие")
        self.log(p, *res)
        return res

    # ---------- журнал ----------

    def log(self, p: Pending, ok: bool, out: str) -> None:
        line = "\t".join([time.strftime("%Y-%m-%d %H:%M:%S"), str(p.chat_id), describe(p),
                          "ok" if ok else "error", out.replace("\n", " ")[:300]])
        try:
            with open(self.log_path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError:
            pass

    def last(self, n: int = 10) -> list[list[str]]:
        try:
            lines = self.log_path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        return [ln.split("\t") for ln in lines[-n:]][::-1]


def describe(p: Pending) -> str:
    return {
        "ctr": f"перезапуск контейнера {p.arg}",
        "svc": f"перезапуск службы {p.arg}",
        "kill": f"остановка процесса {p.extra.get('name', '?')} (PID {p.arg})",
        "clean": "очистка места",
        "reboot": "перезагрузка сервера",
    }.get(p.kind, p.kind)


def top_for_kill(n: int = 8) -> list[tuple[int, str, float, float]]:
    """[(pid, name, cpu%, create_time)] — кандидаты на остановку, без ядра и системных процессов."""
    procs = []
    for p in psutil.process_iter(["pid", "name", "cmdline", "create_time"]):
        try:
            if p.info["pid"] <= 2 or not p.info["cmdline"]:
                continue
            p.cpu_percent(None)
            procs.append(p)
        except psutil.Error:
            continue
    time.sleep(1)
    rows = []
    for p in procs:
        try:
            name = p.info["name"] or "?"
            if name in ("systemd", "sshd", "sshd-session", "dockerd", "containerd", "systemd-journald"):
                continue
            rows.append((p.info["pid"], name, p.cpu_percent(None), p.info["create_time"]))
        except psutil.Error:
            continue
    rows.sort(key=lambda r: r[2], reverse=True)
    return rows[:n]
