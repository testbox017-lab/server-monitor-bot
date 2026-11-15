"""Проверки логики без Telegram: python -m unittest discover tests"""
import asyncio
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

TMP = Path(tempfile.mkdtemp())
CONF = TMP / "config.yaml"
CONF.write_text('token: "123456:' + "A" * 35 + '"\nallowed_chats: [42]\nservices: [ssh]\n'
                f'data_dir: "{TMP}"\n', encoding="utf-8")

import checks  # noqa: E402
import history  # noqa: E402
import probes  # noqa: E402
from settings import Settings  # noqa: E402
from types import SimpleNamespace as NS  # noqa: E402
from zoneinfo import ZoneInfo  # noqa: E402

T0 = 1_800_000_000


def fake_sample(cpu, ram, disk, rx, tx):
    return {"cpu": cpu, "ram": NS(percent=ram), "disks": {"/": NS(percent=disk)},
            "net": NS(bytes_recv=rx, bytes_sent=tx), "load": (0.5, 0.4, 0.3)}


def filled_history(path, days=7, step=600):
    h = history.History(path)
    n = days * 86400 // step
    for i in range(n):
        ts = T0 - (n - i) * step
        rx = (i % 500) * 10_000_000  # счётчик иногда «сбрасывается»
        h.record(fake_sample(20 + (i % 10) * 5, 40 + (i % 7), 50 + i * 20 / n, rx, rx // 2), ts)
    return h


def results(**kw):
    base = {"cpu": None, "ram": None, "svc:ssh": None, "reboot_required": None}
    base.update(kw)
    return base


class SettingsTest(unittest.TestCase):
    def test_set_toggle_reset(self):
        s = Settings(CONF)
        s.reset()
        self.assertEqual(s.cfg["thresholds"]["cpu"], 90)
        s.set("thresholds.cpu", 80)
        self.assertTrue(s.toggle_item("containers", "weather-bot"))
        s2 = Settings(CONF)
        self.assertEqual(s2.cfg["thresholds"]["cpu"], 80)
        self.assertEqual(s2.cfg["thresholds"]["ram"], 90)
        self.assertEqual(s2.cfg["containers"], ["weather-bot"])
        self.assertTrue((TMP / "settings.json.bak").exists())
        self.assertFalse(s2.toggle_item("services", "ssh"))
        self.assertEqual(s2.cfg["services"], [])
        with self.assertRaises(KeyError):
            s2.set("token", "x")
        s2.reset()
        self.assertEqual(s2.cfg["services"], ["ssh"])


class StepTest(unittest.TestCase):
    def setUp(self):
        self.cfg = {"thresholds": {"duration_sec": 300}, "remind_min": 60, "tz": "Europe/Kyiv"}
        self.st = checks.AlertState.load(TMP / f"state_{self._testMethodName}.json")
        self.p = mock.patch.object(checks, "oom_kills", return_value=None)
        self.p.start()

    def tearDown(self):
        self.p.stop()

    def run_step(self, now, **kw):
        return checks.step(self.cfg, self.st, now, results(**kw))

    def test_duration_reminder_recovery(self):
        t = 1_800_000_000
        self.assertEqual(self.run_step(t, cpu="CPU 95%"), [])
        a = self.run_step(t + 301, cpu="CPU 95%")
        self.assertEqual([x.kind for x in a], ["alert"])
        self.assertEqual(self.run_step(t + 600, cpu="CPU 95%"), [])
        a = self.run_step(t + 301 + 3600, cpu="CPU 95%")
        self.assertEqual([x.kind for x in a], ["remind"])
        a = self.run_step(t + 4000)
        self.assertEqual([x.kind for x in a], ["recovered"])
        self.assertEqual(self.st.active, [])

    def test_state_persists(self):
        t = 1_800_000_000
        self.run_step(t, **{"svc:ssh": "down"})
        st2 = checks.AlertState.load(self.st.path)
        self.assertEqual(st2.active, ["svc:ssh"])
        self.st = st2
        self.assertEqual(self.run_step(t + 60, **{"svc:ssh": "down"}), [])  # без повтора после перезапуска
        self.assertEqual([x.kind for x in self.run_step(t + 120)], ["recovered"])

    def test_quiet_hours(self):
        self.cfg["quiet_hours"] = {"start": "23:00", "end": "08:00"}
        night = 1_800_000_000 - (1_800_000_000 % 86400) + 1 * 3600  # 01:00 UTC = 03:00 Киев
        self.assertTrue(checks.in_quiet_hours(self.cfg, night))
        self.run_step(night, cpu="hi")
        a = self.run_step(night + 400, cpu="hi", **{"svc:ssh": "down"})
        self.assertEqual([x.key for x in a], ["svc:ssh"])  # критичный проходит, CPU ждёт утра
        morning = night + 6 * 3600  # 09:00 Киев
        self.assertFalse(checks.in_quiet_hours(self.cfg, morning))
        a = self.run_step(morning, cpu="hi", **{"svc:ssh": "down"})
        self.assertEqual([(x.key, x.kind) for x in a], [("cpu", "alert"), ("svc:ssh", "remind")])

    def test_mute(self):
        t = 1_800_000_000
        self.st.mute_until = t + 3600
        self.assertEqual(self.run_step(t, **{"svc:ssh": "down"}), [])
        a = self.run_step(t + 3700, **{"svc:ssh": "down"})
        self.assertEqual([x.kind for x in a], ["alert"])

    def test_removed_key_forgotten(self):
        t = 1_800_000_000
        self.run_step(t, **{"svc:ssh": "down"})
        checks.step(self.cfg, self.st, t + 60, {"cpu": None})
        self.assertEqual(self.st.active, [])

    def test_oom_event(self):
        self.p.stop()
        with mock.patch.object(checks, "oom_kills", side_effect=[3, 3, 5]):
            t = 1_800_000_000
            self.assertEqual(self.run_step(t), [])
            self.assertEqual(self.run_step(t + 60), [])
            self.assertEqual([x.kind for x in self.run_step(t + 120)], ["event"])
        self.p.start()

    def test_next_morning(self):
        cfg = {"tz": "Europe/Kyiv"}
        t = checks.next_morning(cfg, 1_800_000_000)
        self.assertEqual(checks.fmt_local(cfg, t), "08:00")
        self.assertLess(t - 1_800_000_000, 86400)


class DockerEventsTest(unittest.TestCase):
    def setUp(self):
        self.cfg = {"containers": ["weather-bot"], "log_alert_min": 15}
        self.st = checks.AlertState()

    def ev(self, now, started="t1", restarts=0, logs="", recovered=(), quiet=False):
        info = {"weather-bot": {"restarts": restarts, "started": started, "health": ""}}
        return checks.docker_events(self.cfg, self.st, now, set(recovered), False, quiet, info,
                                    lambda n, since: logs)

    def test_restart_detected_once(self):
        t = 1_800_000_000
        self.assertEqual(self.ev(t), [])                       # первый раз только запоминаем
        self.assertEqual(self.ev(t + 60), [])
        a = self.ev(t + 120, started="t2", restarts=1)
        self.assertEqual(len(a), 1)
        self.assertIn("перезапустился", a[0].text)
        self.assertIn("автоперезапуск", a[0].text)
        self.assertEqual(a[0].key, "log:weather-bot")
        self.assertEqual(self.ev(t + 180, started="t2", restarts=1), [])

    def test_restart_not_duplicated_after_recovery(self):
        t = 1_800_000_000
        self.ev(t)
        self.assertEqual(self.ev(t + 60, started="t2", recovered={"ctr:weather-bot"}), [])

    def test_log_errors_rate_limited(self):
        t = 1_800_000_000
        self.assertEqual(self.ev(t, logs="Traceback boom"), [])  # первый запуск: старое не шлём
        a = self.ev(t + 60, logs="ok\nTraceback (most recent call last)\nValueError: x\nERROR db")
        self.assertEqual(len(a), 1)
        self.assertIn("2 строк", a[0].text)
        self.assertEqual(self.ev(t + 120, logs="ERROR again"), [])  # 15 мин не прошло
        self.assertEqual(len(self.ev(t + 60 + 901, logs="ERROR again")), 1)
        self.assertEqual(self.ev(t + 3000, logs="all good"), [])

    def test_log_alerts_off_and_quiet(self):
        t = 1_800_000_000
        self.ev(t)
        self.assertEqual(self.ev(t + 60, logs="ERROR", quiet=True), [])
        self.cfg["log_alerts"] = False
        self.assertEqual(self.ev(t + 2000, logs="ERROR"), [])

    def test_auto_containers(self):
        cfg = {"containers": ["a"], "containers_auto": True}
        st = checks.AlertState(seen_containers=["a", "b"])
        self.assertEqual(checks.watched_containers(cfg, st), ["a", "b"])
        cfg["containers_auto"] = False
        self.assertEqual(checks.watched_containers(cfg, st), ["a"])
        with mock.patch.object(checks, "container_states", return_value={"c": "running", "d": "exited"}):
            checks.update_seen_containers(st)
        self.assertEqual(st.seen_containers, ["a", "b", "c"])

    def test_container_info_parse(self):
        out = mock.Mock(stdout="/weather-bot\t2\t2026-11-15T07:00:00Z\thealthy\n/x\tbad\t\t\n")
        with mock.patch.object(checks, "docker", return_value=out):
            info = checks.container_info(["weather-bot", "x"])
        self.assertEqual(info["weather-bot"], {"restarts": 2, "started": "2026-11-15T07:00:00Z", "health": "healthy"})
        self.assertEqual(info["x"]["restarts"], 0)

    def test_unhealthy_is_problem(self):
        cfg = {"containers": ["w"], "thresholds": {}}
        with mock.patch.object(checks, "container_states", return_value={"w": "running"}), \
             mock.patch.object(checks, "container_info", return_value={"w": {"health": "unhealthy"}}):
            res = checks.evaluate(cfg)
        self.assertIn("unhealthy", res["ctr:w"])


class HistoryTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.h = filled_history(TMP / "hist_test.db")

    def test_peaks_and_traffic(self):
        p = self.h.peaks(86400, T0)
        self.assertEqual(p["cpu"][0], 65)
        self.assertEqual(p["ram"][0], 46)
        self.assertGreater(p["rx"], 0)
        self.assertLess(p["disk_from"], p["disk_to"])

    def test_forecast(self):
        days = self.h.disk_forecast(T0)
        # 20% за 7 дней ≈ 2.86%/сут, осталось ~30% → ~10.5 дней
        self.assertAlmostEqual(days, 10.5, delta=1.0)

    def test_forecast_flat_and_short(self):
        h = history.History(TMP / "flat.db")
        for i in range(100):
            h.record(fake_sample(10, 10, 40.0, i, i), T0 - (100 - i) * 600)
        self.assertIsNone(h.disk_forecast(T0))
        h2 = history.History(TMP / "short.db")
        for i in range(5):
            h2.record(fake_sample(10, 10, 40 + i, i, i), T0 - (5 - i) * 60)
        self.assertIsNone(h2.disk_forecast(T0))

    def test_retention(self):
        h = history.History(TMP / "ret.db")
        h.record(fake_sample(1, 1, 1, 1, 1), T0 - 40 * 86400)
        h._last_cleanup = 0
        h.record(fake_sample(1, 1, 1, 1, 1), T0)
        self.assertEqual(len(h.rows(0, T0 + 1)), 1)

    def test_chart_png(self):
        for period in history.PERIODS:
            png = history.chart_png(self.h, period, ZoneInfo("Europe/Kyiv"), "srv", T0)
            self.assertTrue(png.startswith(b"\x89PNG"), period)
        (TMP / "chart_7d.png").write_bytes(history.chart_png(self.h, "7d", ZoneInfo("Europe/Kyiv"), "srv", T0))
        self.assertIsNone(history.chart_png(history.History(TMP / "empty.db"), "1h", ZoneInfo("UTC"), "srv", T0))

    def test_caption(self):
        c = history.chart_caption(self.h, "7d", T0)
        self.assertIn("Пик CPU 65%", c)
        self.assertIn("диск заполнится", c)

    def test_bucket(self):
        rows = self.h.rows(0, T0)
        self.assertEqual(len(history._bucket(rows, 100)), 100)

    def test_summary_with_history(self):
        st = checks.AlertState(log=[[T0 - 100, "restart:w"], [T0 - 50, "cpu"]])
        with mock.patch.object(checks, "status_text", return_value="STATUS"):
            t = checks.summary_text({"tz": "Europe/Kyiv"}, st, T0, self.h)
        self.assertIn("Пик CPU: 65%", t)
        self.assertIn("Перезапусков контейнеров: 1", t)
        self.assertIn("диск заполнится", t)


import importlib.machinery  # noqa: E402
import importlib.util  # noqa: E402
import subprocess  # noqa: E402

import actions  # noqa: E402


def load_helper():
    path = str(ROOT / "deploy" / "monitor-bot-action")
    loader = importlib.machinery.SourceFileLoader("monitor_bot_action", path)
    spec = importlib.util.spec_from_loader("monitor_bot_action", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


class HelperScriptTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.h = load_helper()
        allow = TMP / "allowed-services"
        allow.write_text("# comment\ndocker\ncron\n")
        cls.h.ALLOWED_FILE = str(allow)

    def code(self, *argv):
        with mock.patch("builtins.print"), self.assertRaises(SystemExit) as e:
            self.h.main(list(argv))
        return e.exception.code

    def test_restart_service_allowlist(self):
        with mock.patch.object(self.h, "run") as run:
            self.assertEqual(self.code("restart-service", "ssh"), 2)
            self.assertEqual(self.code("restart-service", "docker;rm -rf /"), 2)
            run.assert_not_called()
            run.return_value = mock.Mock(returncode=0, stdout="active", stderr="")
            with mock.patch("builtins.print"):
                self.h.main(["restart-service", "cron"])
            self.assertEqual(run.call_args_list[0].args[0], ["systemctl", "restart", "cron"])

    def test_kill_rules(self):
        self.assertEqual(self.code("kill", "1"), 2)
        self.assertEqual(self.code("kill", "-1"), 2)
        self.assertEqual(self.code("kill", "abc"), 2)
        self.assertEqual(self.code("kill", "2"), 2)
        with mock.patch.object(self.h, "proc_name", return_value="sshd"), \
             mock.patch("builtins.open", mock.mock_open(read_data=b"/usr/sbin/sshd")):
            self.assertEqual(self.code("kill", "12345"), 2)
        child = subprocess.Popen(["sleep", "30"])
        try:
            with mock.patch("builtins.print"):
                self.h.main(["kill", str(child.pid)])
            self.assertEqual(child.wait(5), -15)
        finally:
            child.kill()

    def test_unknown_and_extra_args(self):
        self.assertEqual(self.code("rm", "-rf"), 2)
        self.assertEqual(self.code("reboot", "now"), 2)
        self.assertEqual(self.code("cleanup", "--all"), 2)


class ActionsTest(unittest.TestCase):
    def setUp(self):
        self.a = actions.Actions(TMP / f"actions_{self._testMethodName}.log", helper="/nonexistent")

    def test_confirmations(self):
        t = self.a.ask("ctr", "w", 42)
        self.assertIsNone(self.a.take(t, 7))          # чужой чат
        self.assertIsNotNone(self.a.take(t, 42))
        self.a.pending[t].expires = time.time() - 1
        self.assertIsNone(self.a.take(t, 42))         # истекло
        self.assertNotIn(t, self.a.pending)

    def test_execute_and_log(self):
        with mock.patch.object(actions.subprocess, "run",
                               return_value=mock.Mock(returncode=0, stdout="w\n", stderr="")) as run:
            ok, _ = self.a.execute(actions.Pending("ctr", "w", 42))
        self.assertTrue(ok)
        self.assertEqual(run.call_args.args[0], ["docker", "restart", "w"])
        with mock.patch.object(actions.subprocess, "run",
                               return_value=mock.Mock(returncode=1, stdout="",
                                                      stderr="sudo: a password is required")) as run:
            ok, out = self.a.execute(actions.Pending("svc", "cron", 42))
        self.assertFalse(ok)
        self.assertIn("нет прав", out)
        self.assertEqual(run.call_args.args[0], ["sudo", "-n", "/nonexistent", "restart-service", "cron"])
        log = self.a.last()
        self.assertEqual(len(log), 2)
        self.assertEqual(log[0][3], "error")
        self.assertIn("cron", log[0][2])

    def test_kill_pid_reuse(self):
        child = subprocess.Popen(["sleep", "30"])
        try:
            p = actions.psutil.Process(child.pid)
            ok, out = self.a.kill(child.pid, "other-name", p.create_time())
            self.assertFalse(ok)
            self.assertIn("PID занят", out)
        finally:
            child.kill()
            child.wait()
        ok, out = self.a.kill(child.pid, "sleep", 0)
        self.assertFalse(ok)


import security  # noqa: E402

SSH_LOG = """Accepted publickey for root from 203.0.113.5 port 51122 ssh2: ED25519 SHA256:xx
Failed password for invalid user admin from 198.51.100.7 port 4242 ssh2
Invalid user test from 198.51.100.7 port 4243
Failed password for root from 198.51.100.9 port 1 ssh2
Connection closed by 198.51.100.7 port 4243 [preauth]
"""

SS_OUT = """tcp LISTEN 0 4096 0.0.0.0:22 0.0.0.0:*
tcp LISTEN 0 4096 [::]:22 [::]:*
tcp LISTEN 0 4096 127.0.0.53%lo:53 0.0.0.0:*
tcp LISTEN 0 4096 127.0.0.1:35217 0.0.0.0:*
udp UNCONN 0 0 [::1]:323 [::]:*
tcp LISTEN 0 511 0.0.0.0:8080 0.0.0.0:*
"""


class SecurityTest(unittest.TestCase):
    def setUp(self):
        self.st = checks.AlertState()
        self.cfg = {"ssh_fail_threshold": 3}
        self.ssh = ""
        self.ports = {"tcp/22"}
        self.updates = []

    def ev(self, now, muted=False, quiet=False):
        return security.events(self.cfg, self.st, now, muted, quiet,
                               ssh_text_fn=lambda since: self.ssh, ports_fn=lambda: set(self.ports),
                               updates_fn=lambda: list(self.updates))

    def test_parse(self):
        acc, failed = security.parse_ssh(SSH_LOG)
        self.assertEqual(acc, [("root", "203.0.113.5", "publickey")])
        self.assertEqual(failed, {"198.51.100.7": 2, "198.51.100.9": 1})

    def test_ports_parse(self):
        with mock.patch.object(security, "run", return_value=SS_OUT):
            self.assertEqual(security.listening_ports(), {"tcp/22", "tcp/8080"})

    def test_ssh_new_ip_then_known(self):
        self.ssh = SSH_LOG
        self.assertEqual(self.ev(T0), [])          # первый запуск: только отметка времени
        a = self.ev(T0 + 60)
        texts = [x.text for x in a]
        self.assertTrue(any("нового IP 203.0.113.5" in t for t in texts), texts)
        self.assertTrue(any("Подбор пароля" in t and "198.51.100.7 (2)" in t for t in texts), texts)
        a = self.ev(T0 + 120)                       # тот же IP — уже известен, подбор — не чаще раза в час
        self.assertEqual(a, [])
        self.cfg["ssh_alert_all_logins"] = True
        self.assertTrue(any("Вход по SSH" in x.text for x in self.ev(T0 + 180)))

    def test_bruteforce_hourly_and_window(self):
        self.ssh = "Failed password for root from 1.1.1.1 port 1 ssh2\n" * 2
        self.ev(T0)
        self.assertEqual(self.ev(T0 + 60), [])     # 2 < 3
        self.assertEqual(len(self.ev(T0 + 120)), 1)  # 4 >= 3
        self.assertEqual(self.ev(T0 + 180), [])
        self.assertEqual(self.ev(T0 + 3700), [])            # окно 10 мин: старые попытки уже не считаются
        self.assertEqual(len(self.ev(T0 + 3760)), 1)        # снова ≥3, и прошёл час
        self.ssh = ""
        self.ev(T0 + 9000)
        self.assertEqual(self.st.ssh_failed, [])   # старше 10 минут забываются

    def test_ports_baseline_and_new(self):
        self.assertEqual(self.ev(T0), [])
        self.ports = {"tcp/22", "tcp/8080"}
        a = self.ev(T0 + 60)
        self.assertEqual([x.text for x in a], ["🚪 Новый открытый наружу порт: tcp/8080"])
        self.assertEqual(self.ev(T0 + 120), [])

    def test_updates_daily(self):
        self.updates = ["openssl", "libc6"]
        self.assertEqual(len(self.ev(T0)), 1)
        self.assertEqual(self.ev(T0 + 7 * 3600), [])        # проверено, но напоминание раз в сутки
        self.assertEqual(len(self.ev(T0 + 25 * 3600)), 1)

    def test_muted_and_quiet(self):
        self.ssh = SSH_LOG
        self.ev(T0)
        self.assertEqual(self.ev(T0 + 60, muted=True), [])
        self.assertEqual(self.st.ssh_checked, T0 + 60)     # отметка сдвинулась, старое после тишины не прилетит
        self.st.ssh_checked = T0 + 100
        a = self.ev(T0 + 200, quiet=True)
        self.assertTrue(all("Подбор" not in x.text for x in a))  # подбор ночью молчит, вход приходит

    def test_heartbeat(self):
        resp = mock.MagicMock(status=200)
        resp.__enter__.return_value = resp
        with mock.patch.object(security.urllib.request, "urlopen", return_value=resp):
            self.assertTrue(security.heartbeat("https://hc-ping.com/x"))
        with mock.patch.object(security.urllib.request, "urlopen", side_effect=OSError):
            self.assertFalse(security.heartbeat("https://hc-ping.com/x"))


class ProbesTest(unittest.TestCase):
    def setUp(self):
        self.st = checks.AlertState()
        self.cfg = {"probes": ["https://site.ru", "tcp://db.local:5432"], "probe_fail_count": 2,
                    "ssl_alerts": True, "ssl_warn_days": 14}
        self.res = {}

    def run_fn(self, raws, timeout):
        return {r: self.res.get(r, (True, "HTTP 200", 50)) for r in raws}

    def test_parse_and_normalize(self):
        self.assertEqual(probes.normalize("site.ru"), "https://site.ru")
        self.assertEqual(probes.normalize("db.local:5432"), "tcp://db.local:5432")
        self.assertEqual(probes.normalize("https://site.ru/health | OK"), "https://site.ru/health | OK")
        self.assertEqual(probes.normalize("ping://1.1.1.1"), "ping://1.1.1.1")
        self.assertEqual(probes.parse("https://site.ru:8443").port, 8443)
        for bad in ("", "ftp://x", "tcp://host", "dns://x.ru | OK", "a b c", "x" * 300, "https://"):
            self.assertIsNone(probes.normalize(bad), bad)

    def test_fail_needs_two_in_a_row(self):
        self.res["https://site.ru"] = (False, "HTTP 502", 30)
        r = probes.evaluate(self.cfg, self.st, T0, self.run_fn)
        self.assertIsNone(r["web:https://site.ru"])          # одна неудача — ещё не падение
        r = probes.evaluate(self.cfg, self.st, T0 + 60, self.run_fn)
        self.assertIn("HTTP 502", r["web:https://site.ru"])
        self.assertIsNone(r["web:tcp://db.local:5432"])
        self.res.clear()
        r = probes.evaluate(self.cfg, self.st, T0 + 120, self.run_fn)
        self.assertIsNone(r["web:https://site.ru"])
        self.assertEqual(self.st.probe_fails["https://site.ru"], 0)

    def test_alert_via_step_is_critical(self):
        self.assertTrue(checks.is_critical("web:https://site.ru"))
        st = checks.AlertState()
        cfg = {"thresholds": {"duration_sec": 300}, "remind_min": 0,
               "quiet_hours": {"start": "00:00", "end": "23:59"}, "tz": "UTC"}
        a = checks.step(cfg, st, T0, results(**{"web:https://site.ru": "🌐 Не отвечает https://site.ru: HTTP 502"}))
        self.assertTrue(any("Не отвечает" in x.text for x in a))   # приходит и в тихие часы

    def test_removed_target_forgotten(self):
        self.res["https://site.ru"] = (False, "x", 0)
        probes.evaluate(self.cfg, self.st, T0, self.run_fn)
        self.cfg["probes"] = ["tcp://db.local:5432"]
        probes.evaluate(self.cfg, self.st, T0 + 60, self.run_fn)
        self.assertNotIn("https://site.ru", self.st.probe_fails)

    def test_run_all_parallel_and_bad(self):
        out = probes.run_all(["https://a.ru", "https://b.ru", "ftp://x"], 1,
                             check_fn=lambda t, timeout: (True, t.host, 1))
        self.assertEqual(out["https://a.ru"], (True, "a.ru", 1))
        self.assertFalse(out["ftp://x"][0])

    def test_http_check(self):
        resp = mock.MagicMock(status=200)
        resp.__enter__.return_value = resp
        resp.read.return_value = b"status: OK"
        t = probes.parse("https://site.ru | OK")
        with mock.patch.object(probes.urllib.request, "urlopen", return_value=resp):
            self.assertTrue(probes.check_http(t, 1)[0])
            self.assertFalse(probes.check_http(probes.parse("https://site.ru | FINE"), 1)[0])
        err = probes.urllib.error.HTTPError("https://site.ru", 503, "x", {}, None)
        with mock.patch.object(probes.urllib.request, "urlopen", side_effect=err):
            self.assertEqual(probes.check_http(t, 1), (False, "HTTP 503"))

    def test_tcp_check_real_socket(self):
        import socket
        srv = socket.socket()
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        port = srv.getsockname()[1]
        self.assertTrue(probes.check_tcp(probes.parse(f"tcp://127.0.0.1:{port}"), 2)[0])
        srv.close()
        self.assertFalse(probes.check_tcp(probes.parse(f"tcp://127.0.0.1:{port}"), 2)[0])

    def test_ssl_steps(self):
        days = {"v": 30.0}
        ev = lambda now, **kw: probes.ssl_events(self.cfg, self.st, now, kw.get("muted", False),
                                                 kw.get("quiet", False), days_fn=lambda h, p: days["v"])
        self.assertEqual(ev(T0), [])
        days["v"] = 13.2
        self.assertEqual(ev(T0 + 3600), [])                  # перепроверка раз в 6 часов
        a = ev(T0 + 7 * 3600)
        self.assertEqual(len(a), 1)
        self.assertIn("через 13 дн", a[0].text)
        self.assertEqual(ev(T0 + 14 * 3600), [])             # тот же порог — молчим
        days["v"] = 6.5
        self.assertEqual(ev(T0 + 21 * 3600, quiet=True), [])  # ночью ждём
        self.assertEqual(len(ev(T0 + 22 * 3600)), 1)          # утром приходит
        days["v"] = -0.5
        self.assertIn("истёк", ev(T0 + 29 * 3600)[0].text)
        days["v"] = 89
        ev(T0 + 36 * 3600)
        self.assertNotIn("site.ru", self.st.ssl_alerted)     # продлили — счётчик сброшен
        self.cfg["probes"] = []
        ev(T0 + 43 * 3600)
        self.assertEqual(self.st.ssl_days, {})

    def test_probes_text(self):
        probes.evaluate(self.cfg, self.st, T0, self.run_fn)
        self.st.ssl_days["site.ru"] = 40
        text = probes.probes_text(self.cfg, self.st)
        self.assertIn("✅ https://site.ru — HTTP 200, 50 мс, SSL ещё 40 дн.", text)


class BotButtonsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        sys.argv = ["bot.py", str(CONF)]
        import bot
        cls.bot = bot
        bot.S.reset()

    def press(self, data, chat=42):
        q = mock.MagicMock()
        q.data = data
        q.answer = mock.AsyncMock()
        q.edit_message_text = mock.AsyncMock()
        q.edit_message_media = mock.AsyncMock()
        upd = mock.MagicMock()
        upd.effective_message.reply_photo = mock.AsyncMock()
        upd.callback_query = q
        upd.effective_chat.id = chat
        upd.effective_message.reply_text = mock.AsyncMock()
        ctx = mock.MagicMock()
        with mock.patch.object(self.bot, "reschedule"):
            asyncio.run(self.bot.on_button(upd, ctx))
        return q, upd

    def all_callbacks(self, markup):
        return [b.callback_data for row in markup.inline_keyboard for b in row]

    def test_callback_lengths(self):
        b = self.bot
        views = [b.status_kb(), b.procs_kb("cpu", 10), b.services_kb(), b.alert_kb(), b.alerts_kb(),
                 b.settings_kb()] + [f()[1] for f in (b.thresholds_view, b.interval_view, b.watch_view,
                                                       b.quiet_view, b.remind_view, b.summary_view)]
        for m in views:
            for d in self.all_callbacks(m):
                self.assertLessEqual(len(d.encode()), 64, d)

    def test_every_button_works(self):
        b = self.bot
        seen = set()
        todo = self.all_callbacks(b.settings_kb()) + ["status", "procs:mem:20", "services", "alerts",
                                                       "mute:3600", "mute:off", "mute:morning:a",
                                                       "logs:weather-bot", "logs:weather-bot:100",
                                                       "dk:auto", "dk:logs", "dk:logs", "dk:every:60"]
        with mock.patch.object(checks, "running_services", return_value=["ssh", "cron", "docker"]), \
             mock.patch.object(checks, "container_states", return_value={"weather-bot": "running"}), \
             mock.patch.object(checks, "container_info", return_value={}), \
             mock.patch.object(checks, "container_stats", return_value=[("weather-bot", "0.5%", "50MiB")]), \
             mock.patch.object(checks, "container_logs", return_value="line <b>1</b>"):
            while todo:
                d = todo.pop()
                if d in seen or d == "set:reset":
                    continue
                seen.add(d)
                q, upd = self.press(d)
                q.answer.assert_awaited()
                note = q.answer.await_args.args[0] if q.answer.await_args.args else None
                self.assertNotEqual(note, "Ошибка, подробности в журнале бота", d)
                if q.edit_message_text.await_args:
                    markup = q.edit_message_text.await_args.kwargs.get("reply_markup")
                    if markup and d.startswith(("set", "add")):
                        todo += [x for x in self.all_callbacks(markup) if not x.startswith(("tg", "th:", "int"))]
        self.assertGreater(len(seen), 15)

    def test_settings_changes(self):
        b = self.bot
        b.S.reset()
        self.press("th:cpu:-5")
        self.assertEqual(b.CFG["thresholds"]["cpu"], 85)
        self.press("quiet:23:00-08:00")
        self.assertEqual(b.CFG["quiet_hours"], {"start": "23:00", "end": "08:00"})
        self.press("summary:09:00")
        self.assertEqual(b.CFG["daily_summary"], "09:00")
        self.press("tg:containers:weather-bot:add")
        self.assertIn("weather-bot", b.CFG["containers"])
        self.press("int:300")
        self.assertEqual(b.CFG["check_interval_sec"], 300)
        saved = json.loads((TMP / "settings.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["remind_min"] if "remind_min" in saved else 60, 60)
        self.press("set:reset")
        self.assertEqual(b.CFG["thresholds"]["cpu"], 90)

    def test_alert_kb_logs_button(self):
        b = self.bot
        self.assertIn("logs:weather-bot", self.all_callbacks(b.alert_kb("ctr:weather-bot")))
        self.assertIn("logs:weather-bot", self.all_callbacks(b.alert_kb("log:weather-bot")))
        self.assertNotIn("logs:cpu", self.all_callbacks(b.alert_kb("cpu")))

    def test_logs_text_escaped(self):
        with mock.patch.object(checks, "container_logs", return_value="<script>" + "x" * 5000):
            t = checks.logs_text("w")
        self.assertNotIn("<script>", t)
        self.assertLess(len(t), 4096)

    def test_docker_settings(self):
        b = self.bot
        b.S.reset()
        self.press("dk:auto")
        self.assertTrue(b.CFG["containers_auto"])
        self.press("dk:every:60")
        self.assertEqual(b.CFG["log_alert_min"], 60)
        b.S.reset()
        self.assertFalse(b.CFG["containers_auto"])

    def test_chart_buttons(self):
        b = self.bot
        q, upd = self.press("chart:24h")  # истории ещё нет — текст вместо графика
        upd.effective_message.reply_text.assert_awaited()
        self.assertIn("chart:24h", self.all_callbacks(b.status_kb()))
        with mock.patch.object(b, "HIST", filled_history(TMP / "bot_hist.db")), \
             mock.patch.object(history.time, "time", return_value=T0):
            q, upd = self.press("chart:7d")
            upd.effective_message.reply_photo.assert_awaited()
            self.assertTrue(upd.effective_message.reply_photo.await_args.args[0].startswith(b"\x89PNG"))
            q, upd = self.press("chart:24h:e")
            q.edit_message_media.assert_awaited()
            self.assertEqual(q.answer.await_args.args, (None,))

    def last_markup(self, upd):
        return upd.effective_message.reply_text.await_args.kwargs.get("reply_markup") \
            or upd.effective_message.reply_text.await_args.args[1]

    def token_from(self, upd):
        markup = upd.effective_message.reply_text.await_args.kwargs["reply_markup"]
        return markup.inline_keyboard[0][0].callback_data.split(":")[1]

    def test_action_restart_container_flow(self):
        b = self.bot
        q, upd = self.press("act:ctr:weather-bot")
        token = self.token_from(upd)
        with mock.patch.object(b.ACT, "restart_container", return_value=(True, "weather-bot")) as rc:
            q, upd = self.press(f"do:{token}")
        rc.assert_called_once_with("weather-bot")
        self.assertIn("✅", q.edit_message_text.await_args.args[0])
        q, upd = self.press(f"do:{token}")  # повторное нажатие не выполняет второй раз
        self.assertIn("устарело", q.edit_message_text.await_args.args[0])

    def test_action_reboot_needs_two_confirmations(self):
        b = self.bot
        q, upd = self.press("act:reboot")
        token = self.token_from(upd)
        with mock.patch.object(b.ACT, "reboot", return_value=(True, "rebooting")) as rb:
            q, _ = self.press(f"do:{token}")
            rb.assert_not_called()
            self.assertIn("второе", q.edit_message_text.await_args.args[0])
            q, _ = self.press(f"do:{token}", chat=7)  # чужой чат
            rb.assert_not_called()
            q, _ = self.press(f"do:{token}")
            rb.assert_called_once()

    def test_action_cancel_and_service_allowlist(self):
        b = self.bot
        b.CFG["action_services"] = ["cron"]
        try:
            q, upd = self.press("act:svc:ssh")
            self.assertIn("не входит", upd.effective_message.reply_text.await_args.args[0])
            q, upd = self.press("act:svc:cron")
            token = self.token_from(upd)
            with mock.patch.object(b.ACT, "restart_service") as rs:
                q, _ = self.press(f"no:{token}")
                rs.assert_not_called()
            self.assertIn("Отменено", q.edit_message_text.await_args.args[0])
            self.assertIn("act:svc:cron", self.all_callbacks(b.alert_kb("svc:cron")))
            self.assertNotIn("act:svc:ssh", self.all_callbacks(b.alert_kb("svc:ssh")))
            self.assertIn("act:ctr:weather-bot", self.all_callbacks(b.alert_kb("ctr:weather-bot")))
            self.assertIn("act:clean", self.all_callbacks(b.alert_kb("disk:/")))
        finally:
            b.CFG["action_services"] = []

    def test_actions_menu_and_disabled(self):
        b = self.bot
        with mock.patch.object(b.ACT, "cleanup_estimate", return_value="docker Images: 1GB"), \
             mock.patch.object(actions, "top_for_kill", return_value=[(123, "python", 50.0, 1.0)]):
            for d in ("act:menu", "act:clean", "act:killmenu", "act:log"):
                q, upd = self.press(d)
                self.assertNotEqual(q.answer.await_args.args, ("Ошибка, подробности в журнале бота",), d)
        b.CFG["actions_enabled"] = False
        try:
            self.assertNotIn("act:ctr:weather-bot", self.all_callbacks(b.alert_kb("ctr:weather-bot")))
            q, upd = self.press("act:reboot")
            upd.effective_message.reply_text.assert_not_awaited()
        finally:
            b.CFG["actions_enabled"] = True

    def test_security_settings(self):
        b = self.bot
        b.S.reset()
        b.STATE.known_ips = ["203.0.113.5"]
        self.press("sec:ssh_alerts")
        self.assertFalse(b.CFG["ssh_alerts"])
        self.press("sec:ssh_alert_all_logins")
        self.assertTrue(b.CFG["ssh_alert_all_logins"])
        self.press("secthr:100")
        self.assertEqual(b.CFG["ssh_fail_threshold"], 100)
        q, _ = self.press("secforget")
        self.assertEqual(b.STATE.known_ips, [])
        q, _ = self.press("set:sec")
        self.assertIn("Безопасность", q.edit_message_text.await_args.args[0])
        b.S.reset()

    def test_probe_command_and_buttons(self):
        b = self.bot
        b.S.reset()
        upd = mock.MagicMock()
        upd.effective_chat.id = 42
        upd.effective_message.reply_text = mock.AsyncMock()
        ctx = mock.MagicMock()
        ctx.args = ["site.ru"]
        with mock.patch.object(probes, "run_all", return_value={"https://site.ru": (True, "HTTP 200", 10)}), \
             mock.patch.object(probes, "ssl_days_left", return_value=50.0):
            asyncio.run(b.cmd_probe(upd, ctx))
            self.assertEqual(b.CFG["probes"], ["https://site.ru"])
            ctx.args = ["ftp://x"]
            asyncio.run(b.cmd_probe(upd, ctx))
            self.assertIn("Не понял", upd.effective_message.reply_text.await_args.args[0])
            q, _ = self.press("set:probes")
            text = q.edit_message_text.await_args.args[0]
            self.assertIn("SSL ещё 50 дн.", text)
            cbs = self.all_callbacks(q.edit_message_text.await_args.kwargs["reply_markup"])
            for d in cbs:
                self.assertLessEqual(len(d.encode()), 64, d)
            self.press("pr:run")
            self.press("pr:fails:3")
            self.assertEqual(b.CFG["probe_fail_count"], 3)
            self.press("pr:ssl")
            self.assertFalse(b.CFG["ssl_alerts"])
            self.press([d for d in cbs if d.startswith("pr:del:")][0])
            self.assertEqual(b.CFG["probes"], [])
        b.S.reset()

    def test_stranger_blocked(self):
        b = self.bot
        b.S.reset()
        q, _ = self.press("th:cpu:-5", chat=7)
        self.assertEqual(b.CFG["thresholds"]["cpu"], 90)
        q.edit_message_text.assert_not_awaited()

    def test_reschedule_real(self):
        b = self.bot
        app = b.build_app()
        b.S.set("daily_summary", "09:00")
        b.reschedule(app)
        names = sorted(j.name for j in app.job_queue.jobs() if not j.removed)
        self.assertIn("summary", names)
        self.assertIn("monitor", names)
        b.S.reset()


if __name__ == "__main__":
    unittest.main()
