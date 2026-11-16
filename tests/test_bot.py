"""Проверки логики без Telegram: python -m unittest discover tests"""
import asyncio
import io
import json
import os
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
import backups  # noqa: E402
import network  # noqa: E402
import probes  # noqa: E402
import updater  # noqa: E402
import smart  # noqa: E402
from settings import Settings  # noqa: E402
from types import SimpleNamespace as NS  # noqa: E402
from zoneinfo import ZoneInfo  # noqa: E402

T0 = 1_800_000_000
backups.OFFSITE_STATUS = TMP / "no-offsite-status.json"  # тесты не читают настоящий файл сервера
# На сервере помощник установлен, и docker пошёл бы через sudo. Тесты не должны зависеть от этого.
checks.HELPER = str(TMP / "no-helper")


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
        self.cfg = {"containers": ["weather-bot"], "log_alert_min": 15, "log_check_min": 1}
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
        self.assertEqual(info["weather-bot"], {"restarts": 2, "started": "2026-11-15T07:00:00Z", "health": "healthy",
                                               "reason": ""})
        self.assertEqual(info["x"]["restarts"], 0)
        log = json.dumps([{"ExitCode": 0, "Output": "ok\n"}, {"ExitCode": 1, "Output": "метка не обновлялась 400 с: бот завис\n"}])
        out = mock.Mock(stdout=f"/w\t0\t2026-11-15T07:00:00Z\tunhealthy\t{log}\n")
        with mock.patch.object(checks, "docker", return_value=out):
            self.assertEqual(checks.container_info(["w"])["w"]["reason"], "метка не обновлялась 400 с: бот завис")

    def test_unhealthy_is_problem(self):
        cfg = {"containers": ["w"], "thresholds": {}}
        with mock.patch.object(checks, "container_states", return_value={"w": "running"}), \
             mock.patch.object(checks, "container_info", return_value={"w": {"health": "unhealthy"}}):
            res = checks.evaluate(cfg)
        self.assertIn("завис или не в порядке", res["ctr:w"])
        with mock.patch.object(checks, "container_states", return_value={"w": "running"}), \
             mock.patch.object(checks, "container_info", return_value={"w": {"health": "unhealthy", "reason": "бот завис"}}):
            self.assertTrue(checks.evaluate(cfg)["ctr:w"].endswith(": бот завис"))


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

    def test_backup_status_and_fetch(self):
        h = self.h
        root = Path(tempfile.mkdtemp())
        wb = root / "weather-bot"
        wb.mkdir()
        for name, size, age in (("weather-bot-1.tar.gz", 500, 7200), ("weather-bot-2.tar.gz", 8000, 60),
                                ("latest.tar.gz", 8000, 30)):
            f = wb / name
            f.write_bytes(b"x" * size)
            os.utime(f, (time.time() - age, time.time() - age))
        (wb / "link.tar.gz").symlink_to(wb / "latest.tar.gz")
        watch = root / "backup-watch"
        watch.write_text(f"# c\n{wb} 26\nrelative/path 5\n{root / 'missing'}\n")
        h.WATCH_FILE = str(watch)
        h.OUTBOX = str(root / "outbox")
        out = []
        with mock.patch("builtins.print", side_effect=out.append):
            h.main(["backup-status"])
        data = json.loads(out[0])
        self.assertEqual(len(data), 2)                       # относительный путь отброшен
        self.assertEqual(data[0]["newest"][0], "weather-bot-2.tar.gz")  # latest и ссылки не считаются
        self.assertEqual(data[0]["previous"][0], "weather-bot-1.tar.gz")
        self.assertEqual(data[0]["count"], 2)
        self.assertFalse(data[1]["exists"])
        with mock.patch.object(h.os, "chown"), mock.patch("builtins.print", side_effect=out.append):
            h.main(["backup-fetch", "0"])
            h.main(["backup-fetch", "0"])                    # повтор: старый файл заменяется
        got = json.loads(out[-1])
        self.assertEqual(Path(got["file"]).read_bytes(), b"x" * 8000)
        self.assertEqual(os.listdir(root / "outbox"), ["weather-bot-2.tar.gz"])
        self.assertEqual(self.code("backup-fetch", "5"), 2)
        self.assertEqual(self.code("backup-fetch", "../x"), 2)
        self.assertEqual(self.code("backup-fetch", "1"), 1)  # папки нет
        bad = root / "outbox2"
        bad.symlink_to(root)
        h.OUTBOX = str(bad)
        self.assertEqual(self.code("backup-fetch", "0"), 1)  # подложенная ссылка вместо папки

    def make_tree(self, root, version, extra=None):
        root.mkdir(parents=True)
        (root / "bot.py").write_text(f"V = {version!r}\n")
        (root / "requirements.txt").write_text("psutil\n")
        (root / "tests").mkdir()
        (root / "tests" / "test_x.py").write_text("")
        (root / "deploy").mkdir()
        (root / "deploy" / "monitor-bot-action").write_text(f"# helper {version}\n")
        for k, v in (extra or {}).items():
            (root / k).write_text(v)
        return root

    def update_env(self):
        h = self.h
        base = Path(tempfile.mkdtemp())
        app = self.make_tree(base / "monitor_bot", "old", {"config.yaml": "token: secret\n", "old_only.py": "x"})
        (app / "venv").mkdir()
        (app / "VERSION").write_text(json.dumps({"sha": "a" * 40}))
        new_src = self.make_tree(base / "dl" / "server-monitor-bot-bbb", "new", {"new_module.py": "y"})
        installed_helper = base / "installed-helper"
        installed_helper.write_text("# helper old\n")
        patches = [mock.patch.object(h, "APP_DIR", str(app)), mock.patch.object(h, "RESULT_FILE", str(base / "res.json")),
                   mock.patch.object(h, "LOCK_FILE", str(base / "lock")),
                   mock.patch.object(h, "NOT_AUTO", (("deploy/monitor-bot-action", str(installed_helper)),)),
                   mock.patch.object(h, "download", return_value=str(new_src)),
                   mock.patch.object(h, "check_ownership"), mock.patch.object(h, "check_in_main"),
                   mock.patch.object(h.os, "chown"), mock.patch.object(h, "gid", return_value=0)]
        for p_ in patches:
            p_.start()
            self.addCleanup(p_.stop)
        return base, app

    def test_update_success(self):
        h = self.h
        base, app = self.update_env()
        ok_tests = mock.Mock(returncode=0, stdout="", stderr="Ran 79 tests\n\nOK")
        with mock.patch.object(h.subprocess, "run", return_value=ok_tests), \
             mock.patch.object(h, "healthy_after_restart", return_value=(True, "active")):
            h.update_run("b" * 40)
        res = json.loads((base / "res.json").read_text())
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["from"], "a" * 40)
        self.assertEqual((app / "bot.py").read_text(), "V = 'new'\n")
        self.assertTrue((app / "new_module.py").exists())
        self.assertEqual((app / "config.yaml").read_text(), "token: secret\n")   # конфиг не тронут
        self.assertEqual(json.loads((app / "VERSION").read_text())["sha"], "b" * 40)
        self.assertTrue(any("обновить вручную" in n for n in res["notes"]))
        bak = Path(res["backup"])
        self.assertEqual((bak / "bot.py").read_text(), "V = 'old'\n")
        self.assertFalse((bak / "config.yaml").exists())
        self.assertFalse((base / "lock").exists())

    def test_same_config_ignores_comments(self):
        h = self.h
        d = Path(tempfile.mkdtemp())
        (d / "a.service").write_text("[Service]\n# кэш\nUser=monitor\n\n")
        (d / "b.service").write_text("[Service]\nUser=monitor\n")
        (d / "c.service").write_text("[Service]\nUser=root\n")
        self.assertTrue(h.same_config(str(d / "a.service"), str(d / "b.service")))
        self.assertFalse(h.same_config(str(d / "a.service"), str(d / "c.service")))
        (d / "monitor-bot-action").write_text("# v1\nx\n")
        (d / "inst-monitor-bot-action").write_text("# v2\nx\n")
        self.assertFalse(h.same_config(str(d / "monitor-bot-action"), str(d / "inst-monitor-bot-action")))

    def test_update_tests_fail_changes_nothing(self):
        h = self.h
        base, app = self.update_env()
        bad = mock.Mock(returncode=1, stdout="", stderr="FAILED (failures=1)")
        with mock.patch.object(h.subprocess, "run", return_value=bad), \
             mock.patch.object(h, "healthy_after_restart") as restart:
            h.update_run("b" * 40)
        restart.assert_not_called()
        res = json.loads((base / "res.json").read_text())
        self.assertFalse(res["ok"])
        self.assertIn("тесты", res["error"])
        self.assertEqual((app / "bot.py").read_text(), "V = 'old'\n")

    def test_update_rollback(self):
        h = self.h
        base, app = self.update_env()
        ok_tests = mock.Mock(returncode=0, stdout="", stderr="OK")
        with mock.patch.object(h.subprocess, "run", return_value=ok_tests), \
             mock.patch.object(h, "healthy_after_restart", side_effect=[(False, "activating, автоперезапусков 2"),
                                                                       (True, "active")]):
            h.update_run("b" * 40)
        res = json.loads((base / "res.json").read_text())
        self.assertEqual(res["stage"], "rolled_back")
        self.assertEqual((app / "bot.py").read_text(), "V = 'old'\n")
        self.assertFalse((app / "new_module.py").exists())
        self.assertTrue((app / "old_only.py").exists())
        self.assertEqual(json.loads((app / "VERSION").read_text())["sha"], "a" * 40)

    def test_update_args_and_lock(self):
        h = self.h
        self.assertEqual(self.code("update", "main"), 2)
        self.assertEqual(self.code("update", "b" * 39), 2)
        self.assertEqual(self.code("update-run", "../../etc"), 2)
        base, _ = self.update_env()
        (base / "lock").write_text("")
        self.assertEqual(self.code("update", "b" * 40), 1)
        with mock.patch.object(h, "run", return_value=mock.Mock(returncode=0, stdout="", stderr="")) as run:
            (base / "lock").unlink()
            with mock.patch("builtins.print"):
                h.main(["update", "b" * 40])
        cmd = run.call_args.args[0]
        self.assertEqual(cmd[0], "systemd-run")
        self.assertEqual(cmd[-2:], ["update-run", "b" * 40])

    def test_download_extracts_single_dir(self):
        import io
        import tarfile as tf
        h = self.h
        buf = io.BytesIO()
        with tf.open(fileobj=buf, mode="w:gz") as t:
            data = b"print(1)"
            info = tf.TarInfo("server-monitor-bot-abc/bot.py")
            info.size = len(data)
            t.addfile(info, io.BytesIO(data))
        resp = mock.MagicMock()
        resp.__enter__.return_value = io.BytesIO(buf.getvalue())
        d = tempfile.mkdtemp()
        with mock.patch.object(h.urllib.request, "urlopen", return_value=resp) as uo, \
             mock.patch.object(h, "repo", return_value="o/r"):
            src = h.download("b" * 40, d)
        self.assertEqual(Path(src, "bot.py").read_bytes(), b"print(1)")
        self.assertEqual(uo.call_args.args[0].full_url, "https://codeload.github.com/o/r/tar.gz/" + "b" * 40)

    def test_prune_bak(self):
        h = self.h
        base = Path(tempfile.mkdtemp())
        (base / "monitor_bot").mkdir()
        for i, n in enumerate(("monitor_bot.bak-1", "monitor_bot.bak-2", "monitor_bot.bak-3", "other.bak-4")):
            (base / n).mkdir()
            (base / n / "f").write_text("x" * 100)
            os.utime(base / n, (T0 + i, T0 + i))
        (base / "monitor_bot.bak-link").symlink_to(base / "monitor_bot")
        with mock.patch.object(h, "APP_DIR", str(base / "monitor_bot")), \
             mock.patch.object(h, "du_bytes", return_value=100):
            out = []
            with mock.patch("builtins.print", side_effect=out.append):
                h.main(["bak-list"])
                self.assertEqual([Path(x["path"]).name for x in json.loads(out[0])],
                                 ["monitor_bot.bak-3", "monitor_bot.bak-2", "monitor_bot.bak-1"])
                h.main(["prune-bak", "2"])
            self.assertEqual(json.loads(out[1])["removed"], ["monitor_bot.bak-1"])
            self.assertEqual(self.code("prune-bak", "0"), 2)
            self.assertEqual(self.code("prune-bak", "x"), 2)
        self.assertTrue((base / "other.bak-4").exists())
        self.assertTrue((base / "monitor_bot").exists())

    UFW_STATUS = """Status: active

To                         Action      From
--                         ------      ----
Anywhere                   DENY        185.220.101.5                # monitor-bot
Anywhere                   DENY        94.102.61.1
22/tcp                     ALLOW       Anywhere
Anywhere (v6)              DENY        2001:db8::7                # monitor-bot
"""

    def fake_run(self, peers=""):
        calls = []

        def run(cmd, timeout=300):
            calls.append(cmd)
            if cmd[:2] == ["ufw", "status"]:
                return mock.Mock(returncode=0, stdout=self.UFW_STATUS, stderr="")
            if cmd[0] == "ss":
                return mock.Mock(returncode=0, stdout=peers, stderr="")
            return mock.Mock(returncode=0, stdout="Rule inserted", stderr="")
        return run, calls

    def test_ban_rules(self):
        h = self.h
        run, calls = self.fake_run("0 0 5.161.0.10:22 91.240.118.7:51234\n")
        with mock.patch.object(h, "run", run):
            self.assertEqual(h.banned(), ["185.220.101.5", "2001:db8::7"])   # только поставленные ботом
            self.assertEqual(self.code("ban-ip", "10.0.0.5"), 2)
            self.assertEqual(self.code("ban-ip", "127.0.0.1"), 2)
            self.assertEqual(self.code("ban-ip", "1.2.3.4; rm -rf /"), 2)
            self.assertEqual(self.code("ban-ip", "91.240.118.7"), 2)         # открытая SSH-сессия
            with mock.patch("builtins.print"):
                h.main(["ban-ip", "45.155.205.233"])
            self.assertEqual(calls[-1], ["ufw", "prepend", "deny", "from", "45.155.205.233", "to", "any",
                                         "comment", "monitor-bot"])
            n = len(calls)
            with mock.patch("builtins.print"):
                h.main(["ban-ip", "185.220.101.5"])                          # уже заблокирован
            self.assertFalse(any(c[:2] == ["ufw", "prepend"] for c in calls[n:]))
            self.assertEqual(self.code("unban-ip", "94.102.61.1"), 1)      # блокировал не бот
            with mock.patch("builtins.print"):
                h.main(["unban-ip", "185.220.101.5"])
            self.assertEqual(calls[-1][:3], ["ufw", "delete", "deny"])

    def test_ssh_peers_parse(self):
        h = self.h
        out = "0 0 5.161.0.10:22 91.240.118.7:51234\n0 0 [::ffff:5.161.0.10]:22 [::ffff:80.94.95.3]:4000\n"
        with mock.patch.object(h, "run", return_value=mock.Mock(returncode=0, stdout=out, stderr="")):
            self.assertEqual(h.ssh_peers(), {"91.240.118.7", "80.94.95.3"})

    def test_io_top(self):
        h = self.h
        snaps = [{1: ("a", 0, 0), 2: ("b", 100, 0)}, {1: ("a", 4000, 2000), 2: ("b", 100, 0), 3: ("c", 5, 5)}]
        out = []
        with mock.patch.object(h, "read_io", side_effect=snaps), mock.patch.object(h.time, "sleep"), \
             mock.patch("builtins.print", side_effect=out.append):
            h.main(["io-top"])
        self.assertEqual(json.loads(out[0]), [{"pid": 1, "name": "a", "read": 2000.0, "write": 1000.0}])

    def test_unknown_and_extra_args(self):
        self.assertEqual(self.code("rm", "-rf"), 2)
        self.assertEqual(self.code("reboot", "now"), 2)
        self.assertEqual(self.code("cleanup", "--all"), 2)
        self.assertEqual(self.code("backup-status", "x"), 2)


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
        self.assertEqual(acc, [("root", "203.0.113.5", "publickey", "SHA256:xx")])
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
        self.assertIn("brute:198.51.100.7", [x.key for x in a])
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


class SmartTest(unittest.TestCase):
    def setUp(self):
        self.cfg = {"thresholds": {"duration_sec": 300, "cpu": 90, "ram": 90}, "remind_min": 60, "tz": "UTC"}
        self.st = checks.AlertState()
        self.p = mock.patch.object(checks, "oom_kills", return_value=None)
        self.p.start()

    def tearDown(self):
        self.p.stop()

    def run_step(self, now, **kw):
        return checks.step(self.cfg, self.st, now, results(**kw))

    def test_ack_stops_reminders_until_recovered(self):
        self.run_step(T0, **{"svc:ssh": "down"})
        self.st.acked.append("svc:ssh")
        self.assertEqual(self.run_step(T0 + 4000, **{"svc:ssh": "down"}), [])   # напоминания нет
        self.assertEqual([x.kind for x in self.run_step(T0 + 4100)], ["recovered"])
        self.assertEqual(self.st.acked, [])
        a = self.run_step(T0 + 4200, **{"svc:ssh": "down"})                    # упало снова — снова алерт
        self.assertEqual([x.kind for x in a], ["alert"])

    def test_snooze_one_key(self):
        self.run_step(T0, **{"svc:ssh": "down"})
        self.st.snoozed["svc:ssh"] = T0 + 3600
        self.assertEqual(self.run_step(T0 + 3000, **{"svc:ssh": "down"}), [])
        self.run_step(T0 + 3100)                                               # восстановилось
        self.assertEqual(self.run_step(T0 + 3200, **{"svc:ssh": "down"}), [])  # снова упало, но молчим
        a = self.run_step(T0 + 3700, **{"svc:ssh": "down"})
        self.assertEqual([x.kind for x in a], ["alert"])                       # час прошёл
        self.assertEqual(self.st.snoozed, {})

    def test_anomaly_rules(self):
        prof = {"cpu": (10.0, 3.0, 5), "ram": (40.0, 2.0, 2)}
        sample = {"cpu": 50.0, "ram": NS(percent=80.0)}
        tzinfo = ZoneInfo("UTC")
        r = smart.anomaly_results(self.cfg, sample, object(), T0, tzinfo, prof)
        self.assertIn("обычно в этот час около 10%", r["anom:cpu"])
        self.assertIsNone(r["anom:ram"])                      # мало истории (2 дня)
        sample["cpu"] = 30.0
        self.assertIsNone(smart.anomaly_results(self.cfg, sample, object(), T0, tzinfo, prof)["anom:cpu"])
        sample["cpu"] = 95.0                                  # выше порога: будет обычный алерт, не дублируем
        self.assertIsNone(smart.anomaly_results(self.cfg, sample, object(), T0, tzinfo, prof)["anom:cpu"])
        self.cfg["anomaly_alerts"] = False
        self.assertEqual(smart.anomaly_results(self.cfg, sample, object(), T0, tzinfo, prof), {})

    def test_anomaly_needs_15_minutes(self):
        text = "🧐 Необычно высокая CPU"
        self.assertEqual(self.run_step(T0, **{"anom:cpu": text}), [])
        self.assertEqual(self.run_step(T0 + 600, **{"anom:cpu": text}), [])
        a = self.run_step(T0 + 901, **{"anom:cpu": text})
        self.assertEqual([x.kind for x in a], ["alert"])
        a = self.run_step(T0 + 1000, **{"anom:cpu": None})
        self.assertEqual(a[0].text, "✅ Восстановлено: необычная нагрузка CPU")

    def test_hour_profile(self):
        h = history.History(TMP / "smart_profile.db")
        tzinfo = ZoneInfo("UTC")
        now = T0 - T0 % 86400 + 10 * 3600 + 1800        # 10:30 UTC
        for d in range(1, 6):
            for m in range(0, 60, 5):
                ts = now - d * 86400 - 1800 + m * 60    # 10:00–10:55 прошлых дней
                h.record(fake_sample(12, 40, 50, 0, 0), ts)
                h.record(fake_sample(80, 90, 50, 0, 0), ts - 4 * 3600)  # другой час — не считается
        prof = h.hour_profile(now, tzinfo)
        self.assertAlmostEqual(prof["cpu"][0], 12)
        self.assertEqual(prof["cpu"][2], 5)

    def test_ram_trend(self):
        h = history.History(TMP / "smart_trend.db")
        for i in range(0, 6 * 60 + 1, 5):               # 6 часов, RAM от 55% до 79%
            h.record(fake_sample(10, 55 + i * 4 / 60, 50, 0, 0), T0 - 6 * 3600 + i * 60)
        top = lambda: ("java", 2 * 1024 ** 3)
        a = smart.ram_trend_events(self.cfg, self.st, h, T0, False, False, top_fn=top)
        self.assertEqual(len(a), 1)
        self.assertIn("Память растёт: +24% за 6 ч, сейчас 79%", a[0].text)
        self.assertIn("через 5 ч", a[0].text)
        self.assertIn("java (2.0 ГБ)", a[0].text)
        self.assertEqual(smart.ram_trend_events(self.cfg, self.st, h, T0 + 60, False, False, top_fn=top), [])
        st = checks.AlertState()
        self.assertEqual(smart.ram_trend_events(self.cfg, st, h, T0, False, True, top_fn=top), [])  # ночью молчим

    def test_ram_flat_no_alert(self):
        h = history.History(TMP / "smart_flat.db")
        for i in range(0, 361, 5):
            h.record(fake_sample(10, 70 + (i % 10) * 0.1, 50, 0, 0), T0 - 6 * 3600 + i * 60)
        self.assertEqual(smart.ram_trend_events(self.cfg, self.st, h, T0, False, False, top_fn=lambda: None), [])


class BackupsTest(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.conf = self.dir / "config.yaml"
        self.conf.write_text("token: x\n")
        (self.dir / "settings.json").write_text("{}")
        (self.dir / "state.json").write_text("{}")
        history.History(self.dir / "metrics.db").record(fake_sample(10, 20, 30, 0, 0), T0)
        self.st = checks.AlertState()
        self.cfg = {"backup_time": "03:45", "backup_alerts": True}

    def test_make_and_prune(self):
        import tarfile
        for i in range(4):
            path, size, removed = backups.make_backup(self.conf, self.dir, keep=3, now=T0 + i)
        self.assertEqual(removed, 1)
        self.assertEqual(len(backups.own_files(self.dir)), 3)
        with tarfile.open(path) as t:
            names = sorted(t.getnames())
        self.assertEqual(names, ["config.yaml", "data/metrics.db", "data/settings.json", "data/state.json"])
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
        self.assertEqual(os.stat(backups.own_dir(self.dir)).st_mode & 0o777, 0o700)

    def watched(self, newest_age_h, prev_size=8000, new_size=8000, exists=True):
        def fn(now):
            return [{"path": "/var/backups/weather-bot", "max_age_h": 26, "exists": exists, "count": 2,
                     "total": prev_size + new_size,
                     "newest": ["w-2.tar.gz", new_size, now - newest_age_h * 3600] if newest_age_h is not None else None,
                     "previous": ["w-1.tar.gz", prev_size, now - 86400]}]
        return fn

    def test_age_alerts(self):
        now = time.time()
        self.st.bak_first_seen = {"бот": now - 2 * 86400}   # папку наблюдаем давно
        r = backups.evaluate(self.cfg, self.st, now, self.dir, self.watched(3))
        self.assertIsNone(r["bak:/var/backups/weather-bot"])
        self.assertIn("нет ни одной", r["bak:бот"])            # своих копий ещё нет
        backups.make_backup(self.conf, self.dir)
        r = backups.evaluate(self.cfg, self.st, now, self.dir, self.watched(30))
        self.assertIsNone(r["bak:бот"])
        self.assertIn("уже 30 ч", r["bak:/var/backups/weather-bot"])
        r = backups.evaluate(self.cfg, self.st, now, self.dir, self.watched(None, exists=False))
        self.assertIn("нет", r["bak:/var/backups/weather-bot"])
        self.cfg["backup_time"] = None                         # своё расписание выключено — не проверяем
        self.assertNotIn("bak:бот", backups.evaluate(self.cfg, self.st, now, self.dir, self.watched(3)))
        self.cfg["backup_alerts"] = False
        self.assertEqual(backups.evaluate(self.cfg, self.st, now, self.dir, self.watched(30)), {})

    def test_size_drop_once(self):
        now = time.time()
        backups.evaluate(self.cfg, self.st, now, self.dir, self.watched(1, 8000, 900))
        self.assertEqual(len(self.st.bak_events), 1)
        self.assertIn("намного меньше", self.st.bak_events[0])
        backups.evaluate(self.cfg, self.st, now, self.dir, self.watched(1, 8000, 900))
        self.assertEqual(len(self.st.bak_events), 1)          # о той же копии второй раз не пишем

    def test_watched_cache_and_helper_errors(self):
        calls = []

        def helper_fn(*args):
            calls.append(args)
            return True, '[{"path": "/x", "max_age_h": 26, "exists": true, "count": 0, "total": 0, "newest": null, "previous": null}]'
        backups._cache.update(at=0.0, watched=None)
        self.assertEqual(backups.watched_status(T0, helper_fn=helper_fn)[0]["path"], "/x")
        backups.watched_status(T0 + 60, helper_fn=helper_fn)
        self.assertEqual(len(calls), 1)                       # кэш 30 минут
        backups.watched_status(T0 + 60, force=True, helper_fn=lambda *a: (False, "sudo: a password is required"))
        self.assertIsNone(backups._cache["watched"])
        self.assertEqual(backups.fetch(0, helper_fn=lambda *a, **k: (False, "too big: 99"))[1],
                         "файл больше 45 МБ, Telegram его не примет")
        self.assertEqual(backups.fetch(0, helper_fn=lambda *a, **k: (True, '{"file": "/run/x/a.tgz", "size": 1}'))[0],
                         Path("/run/x/a.tgz"))
        backups._cache.update(at=0.0, watched=None)

    def test_status_text(self):
        backups.make_backup(self.conf, self.dir)
        text = backups.status_text(self.cfg, self.dir, watched_fn=self.watched(30))
        self.assertIn("✅ Бот (настройки, состояние, история): последняя", text)
        self.assertIn("❌ /var/backups/weather-bot", text)
        self.assertIn("помощник не настроен", backups.status_text(self.cfg, self.dir, watched_fn=lambda now: None))

    def test_step_sends_events(self):
        st = checks.AlertState()
        st.bak_events = ["⚠️ маленькая копия"]
        cfg = {"thresholds": {}, "remind_min": 0, "tz": "UTC"}
        with mock.patch.object(checks, "evaluate", return_value=results()), \
             mock.patch.object(checks, "oom_kills", return_value=None), \
             mock.patch.object(checks, "update_seen_containers"), \
             mock.patch.object(checks, "docker_events", return_value=[]), \
             mock.patch("security.events", return_value=[]):
            a = checks.step(cfg, st, T0)
        self.assertEqual([x.text for x in a], ["⚠️ маленькая копия"])
        self.assertEqual(st.bak_events, [])


class UpdaterTest(unittest.TestCase):
    def test_version_and_api(self):
        d = Path(tempfile.mkdtemp())
        self.assertEqual(updater.current(d), {})
        (d / "VERSION").write_text(json.dumps({"sha": "c" * 40, "installed": T0}))
        self.assertEqual(updater.short(updater.current(d)["sha"]), "ccccccc")
        (d / "VERSION").write_text('{"sha": "nope"}')
        self.assertEqual(updater.current(d), {})

        def api_fn(path):
            if "/commits/" in path:
                return {"sha": "d" * 40, "commit": {"message": "Этап 11\n\nподробности",
                                                    "committer": {"date": "2026-11-15T10:00:00Z"}}}
            return {"commits": [{"commit": {"message": "Изменение A"}, "parents": [1]},
                                {"commit": {"message": "Merge pull request"}, "parents": [1, 2]}]}
        self.assertEqual(updater.latest("o/r", api_fn=api_fn)["message"], "Этап 11")
        self.assertEqual(updater.changes("o/r", "a", "b", api_fn=api_fn), ["Изменение A"])

    def test_result_text(self):
        self.assertIn("✅ Бот обновлён до bbbbbbb (было aaaaaaa)",
                      updater.result_text({"ok": True, "sha": "b" * 40, "from": "a" * 40, "notes": []}))
        t = updater.result_text({"ok": False, "sha": "b" * 40, "stage": "rolled_back", "error": "не запустилась",
                                 "notes": ["помощник изменился"]})
        self.assertIn("Работает прежняя версия", t)
        self.assertIn("⚠️ помощник изменился", t)
        self.assertIn("Тесты: FAILED", updater.result_text({"ok": False, "sha": "b" * 40, "stage": "tests",
                                                             "error": "тесты не прошли", "tests": "FAILED"}))


class NetworkTest(unittest.TestCase):
    def test_remote_ips(self):
        C = lambda ip, st="ESTABLISHED": NS(status=st, raddr=(ip, 443) if ip else ())
        conns = [C("140.82.121.3"), C("140.82.121.3"), C("::ffff:1.1.1.1"), C("127.0.0.1"), C("10.0.0.2"),
                 C("8.8.8.8", "TIME_WAIT"), C(None)]
        self.assertEqual(network.remote_ips(conns=conns), [("140.82.121.3", 2), ("1.1.1.1", 1)])

    def test_net_text(self):
        h = mock.Mock()
        h.peaks.return_value = {"rx": 5 * 1024 ** 2, "tx": 1024 ** 2}
        text = network.net_text({"disks": ["/"]}, None, lambda *a, **k: (True, '[{"pid": 5, "name": "dockerd", "read": 1024, "write": 2048}]'),
                                rates_fn=lambda: [("eth0", 2048.0, 1024.0)], ips_fn=lambda: [("140.82.121.3", 3)])
        self.assertIn("eth0: ↓ 2.0 КБ/с ↑ 1.0 КБ/с", text)
        self.assertIn("140.82.121.3: 3", text)
        self.assertIn("dockerd (5): 1.0 КБ / 2.0 КБ", text)
        self.assertIn("Inode на /:", text)
        text = network.net_text({}, h, lambda *a, **k: (False, "unknown command"), rates_fn=lambda: [], ips_fn=lambda: [])
        self.assertIn("помощник не обновлён", text)
        self.assertIn("Трафик за сутки", text)

    def test_inode_alert(self):
        with mock.patch.object(network, "inode_usage", return_value={"/": 95.0}):
            r = checks.evaluate({"thresholds": {"inode": 90}, "disks": ["/"]})
        self.assertIn("inode", r["inode:/"])
        with mock.patch.object(network, "inode_usage", return_value={"/": 13.0}):
            r = checks.evaluate({"thresholds": {"inode": 90}, "disks": ["/"]})
        self.assertIsNone(r["inode:/"])


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
        q.edit_message_reply_markup = mock.AsyncMock()
        upd = mock.MagicMock()
        upd.effective_message.reply_photo = mock.AsyncMock()
        upd.callback_query = q
        upd.effective_chat.id = chat
        upd.effective_chat.type = "private"
        upd.effective_message.reply_text = mock.AsyncMock()
        ctx = mock.MagicMock()
        ctx.bot.send_document = mock.AsyncMock()
        ctx.bot.send_message = mock.AsyncMock()
        ctx.bot.send_photo = mock.AsyncMock()
        self.ctx = ctx
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
                        # язык проверяет test_i18n: здесь он сменил бы язык ответов посреди обхода
                        todo += [x for x in self.all_callbacks(markup) if not x.startswith(("tg", "th:", "int", "lang:"))]
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
        upd.effective_chat.type = "private"
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

    def test_ack_snooze_buttons(self):
        b = self.bot
        b.STATE.active = ["web:https://site.ru/very/long/path/that/is/long"]
        key = b.STATE.active[0]
        m = b.alert_kb(key, True)
        cbs = self.all_callbacks(m)
        for d in cbs:
            self.assertLessEqual(len(d.encode()), 64, d)
        ack = [d for d in cbs if d.startswith("ack:")][0]
        q, _ = self.press(ack)
        self.assertIn(key, b.STATE.acked)
        self.assertIn("Принято", q.answer.await_args.args[0])
        new_markup = q.edit_message_reply_markup.await_args.args[0]
        self.assertFalse(any(d.startswith("ack:") for d in self.all_callbacks(new_markup)))
        q, _ = self.press([d for d in cbs if d.startswith("snz:")][0])
        self.assertGreater(b.STATE.snoozed[key], time.time() + 3000)
        self.assertIn("✔️ принято", b.alerts_text())
        q, _ = self.press("ack:0000000000")
        self.assertEqual(q.answer.await_args.args[0], "Эта проблема уже решена")
        b.STATE.acked = []
        self.press("ackall")
        self.assertEqual(b.STATE.acked, [key])
        b.STATE.active, b.STATE.acked, b.STATE.snoozed = [], [], {}

    def test_grouped_alerts(self):
        b = self.bot
        b.S.reset()
        alerts = [checks.Alert(f"🚨 проблема {i} <x>", f"svc:s{i}", "alert") for i in range(5)]
        sent = []

        async def fake_broadcast(bot, text, markup=None, silent=False):
            sent.append((text, markup))
        with mock.patch.object(checks, "step", return_value=alerts), \
             mock.patch.object(updater, "last_result", return_value=None), \
             mock.patch.object(b, "broadcast", fake_broadcast), mock.patch.object(b.HIST, "record"):
            asyncio.run(b.monitor_job(mock.MagicMock()))
            self.assertEqual(len(sent), 1)
            self.assertIn("Сразу 5 событий", sent[0][0])
            self.assertIn("&lt;x&gt;", sent[0][0])
            sent.clear()
            b.S.set("group_alerts", False)
            asyncio.run(b.monitor_job(mock.MagicMock()))
            self.assertEqual(len(sent), 5)
            self.assertTrue(any(d.startswith("ack:") for d in self.all_callbacks(sent[0][1])))
        b.S.reset()

    def test_smart_settings(self):
        b = self.bot
        b.S.reset()
        q, _ = self.press("set:smart")
        self.assertIn("Умные алерты", q.edit_message_text.await_args.args[0])
        self.press("sm:anomaly_alerts")
        self.assertFalse(b.CFG["anomaly_alerts"])
        self.press("sm:group_alerts")
        self.assertFalse(b.CFG["group_alerts"])
        b.S.reset()

    def test_backup_menu(self):
        b = self.bot
        b.S.reset()
        watched = [{"path": "/var/backups/weather-bot", "max_age_h": 26, "exists": True, "count": 1,
                    "total": 10, "newest": ["w.tar.gz", 10, time.time()], "previous": None}]
        out = Path(tempfile.mkdtemp()) / "w.tar.gz"
        out.write_bytes(b"data")
        with mock.patch.object(backups, "watched_status", return_value=watched), \
             mock.patch.object(backups, "fetch", return_value=(out, "")):
            q, upd = self.press("set:backup")
            self.assertIn("Резервные копии", q.edit_message_text.await_args.args[0])
            cbs = self.all_callbacks(q.edit_message_text.await_args.kwargs["reply_markup"])
            self.assertIn("bk:send:0", cbs)
            for d in cbs:
                self.assertLessEqual(len(d.encode()), 64, d)
            q, upd = self.press("bk:send:own")
            self.assertIn("Копий бота ещё нет", upd.effective_message.reply_text.await_args.args[0])
            q, upd = self.press("bk:make")
            self.assertIn("Готово: monitor-bot-", upd.effective_message.reply_text.await_args.args[0])
            self.assertEqual(q.answer.await_count, 1)
            q, _ = self.press("bk:send:own")
            self.ctx.bot.send_document.assert_awaited()
            q, _ = self.press("bk:send:0")
            self.assertEqual(self.ctx.bot.send_document.await_args.kwargs["filename"], "w.tar.gz")
            self.press("bk:time:off")
            self.assertIsNone(b.CFG["backup_time"])
            self.press("bk:time:05:00")
            self.assertEqual(b.CFG["backup_time"], "05:00")
            self.press("bk:daily")
            self.assertTrue(b.CFG["backup_send_daily"])
            # ежедневная задача: копия + отправка
            ctx = mock.MagicMock()
            ctx.bot.send_document = mock.AsyncMock()
            ctx.bot.send_message = mock.AsyncMock()
            asyncio.run(b.backup_job(ctx))
            self.assertEqual(ctx.bot.send_document.await_count, 2)
        with mock.patch.object(backups, "make_backup", side_effect=OSError("disk full")):
            ctx = mock.MagicMock()
            ctx.bot.send_message = mock.AsyncMock()
            asyncio.run(b.backup_job(ctx))
            self.assertIn("disk full", ctx.bot.send_message.await_args.args[1])
        b.S.reset()

    def test_update_menu_flow(self):
        b = self.bot
        b.S.reset()
        b.LATEST.clear()
        with mock.patch.object(updater, "current", return_value={"sha": "a" * 40, "installed": time.time()}), \
             mock.patch.object(updater, "latest", return_value={"sha": "b" * 40, "message": "Этап 11", "date": ""}), \
             mock.patch.object(updater, "changes", return_value=["Обновление кнопкой <b>"]):
            q, _ = self.press("up:check")
            text = q.edit_message_text.await_args.args[0]
            self.assertIn("Доступна версия bbbbbbb", text)
            self.assertIn("&lt;b&gt;", text)
            cbs = self.all_callbacks(q.edit_message_text.await_args.kwargs["reply_markup"])
            self.assertIn("up:go:" + "b" * 40, cbs)
            for d in cbs:
                self.assertLessEqual(len(d.encode()), 64, d)
            q, upd = self.press("up:go:" + "b" * 40)
            markup = upd.effective_message.reply_text.await_args.kwargs["reply_markup"]
            yes = self.all_callbacks(markup)[0]
            with mock.patch.object(b.ACT, "_helper", return_value=(True, "started")) as hp:
                q, _ = self.press(yes)
            hp.assert_called_once_with("update", "b" * 40, timeout=30)
            self.assertIn("запущено", q.edit_message_text.await_args.args[0])
            q, _ = self.press("up:go:notasha")
            self.assertEqual(q.answer.await_args.args[0], "Неверная версия")
        with mock.patch.object(updater, "latest", side_effect=OSError("no network")):
            q, _ = self.press("up:check")
            self.assertIn("Не удалось проверить GitHub", q.edit_message_text.await_args.args[0])
        baks = [{"path": f"/opt/monitor_bot.bak-{i}", "mtime": 0, "size": 1000} for i in range(4)]
        with mock.patch.object(b.ACT, "_helper", return_value=(True, json.dumps(baks))):
            q, upd = self.press("up:prune")
            self.assertIn("Удалить 2 старых копий", upd.effective_message.reply_text.await_args.args[0])
        with mock.patch.object(b.ACT, "_helper", return_value=(True, json.dumps(baks[:2]))):
            q, _ = self.press("up:prune")
            self.assertIn("удалять нечего", q.answer.await_args.args[0])
        b.LATEST.clear()

    def test_report_update_once(self):
        b = self.bot
        sent = []

        async def fake_broadcast(bot, text, markup=None, silent=False):
            sent.append(text)
        res = {"ok": True, "sha": "b" * 40, "from": "a" * 40, "finished": 123.0, "notes": []}
        b.STATE.update_reported = None
        with mock.patch.object(updater, "last_result", return_value=res), mock.patch.object(b, "broadcast", fake_broadcast):
            asyncio.run(b.report_update(None))
            asyncio.run(b.report_update(None))
        self.assertEqual(len(sent), 1)
        self.assertIn("обновлён до bbbbbbb", sent[0])

    def test_menu_commands_registered(self):
        b = self.bot
        names = {n for n, _ in b.COMMANDS}
        app = b.build_app()
        handled = {c for h in app.handlers[0] for c in getattr(h, "commands", ())}
        self.assertTrue(names <= handled, names - handled)
        for n, d in b.COMMANDS:
            self.assertTrue(1 <= len(d) <= 256 and n.islower(), n)
        app2 = mock.MagicMock()
        app2.bot.set_my_commands = mock.AsyncMock()
        app2.bot.send_message = mock.AsyncMock()
        with mock.patch.object(checks, "check_reboot", return_value=None):
            asyncio.run(b.on_startup(app2))
        self.assertEqual(len(app2.bot.set_my_commands.await_args.args[0]), len(b.COMMANDS))

    def test_ban_flow(self):
        b = self.bot
        key = "brute:185.220.101.5"
        cbs = self.all_callbacks(b.alert_kb(key))
        self.assertIn("act:ban:185.220.101.5", cbs)
        q, upd = self.press("act:ban:2a00:1450:4001::7")                    # IPv6 с двоеточиями
        self.assertIn("2a00:1450:4001::7", upd.effective_message.reply_text.await_args.args[0])
        q, upd = self.press("act:ban:185.220.101.5")
        yes = self.all_callbacks(upd.effective_message.reply_text.await_args.kwargs["reply_markup"])[0]
        with mock.patch.object(b.ACT, "_helper", return_value=(False, "с этого IP сейчас открыта SSH-сессия, не блокирую")) as hp:
            q, _ = self.press(yes)
        hp.assert_called_once_with("ban-ip", "185.220.101.5", timeout=30)
        self.assertIn("SSH-сессия", q.edit_message_text.await_args.args[0])
        with mock.patch.object(b.ACT, "_helper", return_value=(True, '["185.220.101.5"]')):
            q, upd = self.press("act:banned")
        m = upd.effective_message.reply_text.await_args.kwargs["reply_markup"]
        self.assertEqual(self.all_callbacks(m), ["act:unban:185.220.101.5"])
        b.STATE.known_ips = ["80.94.95.4"]
        q, upd = self.press("act:ban:80.94.95.4")
        self.assertIn("раньше был успешный вход", upd.effective_message.reply_text.await_args.args[0])
        q, upd = self.press("act:ban:10.1.1.1")
        self.assertIn("Локальные адреса", upd.effective_message.reply_text.await_args.args[0])
        b.STATE.known_ips = []

    def test_net_button(self):
        b = self.bot
        with mock.patch.object(network, "net_text", return_value="🌐 <b>Сеть и диск</b>") as nt:
            q, upd = self.press("net")
            self.assertIn("Сеть и диск", upd.effective_message.reply_text.await_args.args[0])
            q, _ = self.press("net:r")
            q.edit_message_text.assert_awaited()
        self.assertIn("net", self.all_callbacks(b.status_kb()))

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


class OffsiteTest(unittest.TestCase):
    """Этап 13: копии на ПК и проверка, что копии читаются."""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.conf = self.dir / "config.yaml"
        self.conf.write_text("token: x\n")
        (self.dir / "state.json").write_text("{}")
        history.History(self.dir / "metrics.db").record(fake_sample(10, 20, 30, 0, 0), T0)

    def test_offsite_alert(self):
        st, now = checks.AlertState(), time.time()
        cfg = {"backup_alerts": True, "offsite_max_age_h": 72}
        never = backups.evaluate(cfg, st, now, None, lambda n: [], offsite_fn=lambda: None)
        self.assertNotIn("bak:pc", never)                       # ПК ещё не настроен — не шумим
        old = backups.evaluate(cfg, st, now, None, lambda n: [], offsite_fn=lambda: {"ts": now - 80 * 3600})
        self.assertIn("уже 80 ч", old["bak:pc"])
        fresh = backups.evaluate(cfg, st, now, None, lambda n: [], offsite_fn=lambda: {"ts": now - 3600})
        self.assertIsNone(fresh["bak:pc"])
        cfg["offsite_max_age_h"] = None
        self.assertNotIn("bak:pc", backups.evaluate(cfg, st, now, None, lambda n: [], offsite_fn=lambda: {"ts": 0}))
        self.assertEqual(checks.key_label("bak:pc"), "копии на ПК")

    def test_offsite_status_file(self):
        f = self.dir / "status.json"
        self.assertIsNone(backups.offsite_status(f))
        f.write_text('{"ts": 5, "files": [{"name": "a"}], "bytes": 2048}')
        self.assertEqual(backups.offsite_status(f)["ts"], 5)
        text = backups.offsite_text({"offsite_max_age_h": 72}, 5 + 3600, lambda: backups.offsite_status(f))
        self.assertIn("✅ На ПК: забраны", text)
        self.assertIn("ещё ни разу", backups.offsite_text({}, 0, lambda: None))

    def test_verify_own(self):
        self.assertFalse(backups.verify_own(self.dir)["ok"])     # копий нет
        path, _, _ = backups.make_backup(self.conf, self.dir)
        r = backups.verify_own(self.dir)
        self.assertTrue(r["ok"], r)
        self.assertIn("история: 1 записей", r["note"])
        path.write_bytes(path.read_bytes()[:200])               # обрезанная копия
        r = backups.verify_own(self.dir)
        self.assertFalse(r["ok"])
        self.assertTrue(r["error"])

    def test_verify_all_and_text(self):
        backups.make_backup(self.conf, self.dir)
        other = [{"label": "weather-bot", "path": "/var/backups/weather-bot", "name": "w.tar.gz", "size": 10,
                  "ok": False, "members": 0, "error": "EOFError: обрыв"}]
        res, err = backups.verify_all(self.dir, helper_fn=lambda *a, **k: (True, json.dumps(other)))
        self.assertIsNone(err)
        self.assertEqual([r["ok"] for r in res], [True, False])
        self.assertTrue(backups.verify_failed(res))
        text = backups.verify_text(res, err, "15.11 04:30")
        self.assertIn("✅ бот", text)
        self.assertIn("❌ /var/backups/weather-bot: EOFError", text)
        res, err = backups.verify_all(self.dir, helper_fn=lambda *a, **k: (False, "sudo: a password is required"))
        self.assertEqual(len(res), 1)
        self.assertIn("не проверены", backups.verify_text(res, err))


class HelperOffsiteTest(unittest.TestCase):
    def setUp(self):
        import gzip
        import tarfile
        self.h = load_helper()
        base = Path(tempfile.mkdtemp())
        own, w = base / "own", base / "weather-bot"
        own.mkdir()
        w.mkdir()
        good = own / "monitor-bot-2026-11-15_034500.tar.gz"
        with tarfile.open(good, "w:gz") as t:
            src = base / "f.txt"
            src.write_text("hello")
            t.add(src, arcname="config.yaml")
        (own / "junk.txt").write_text("не копия бота")
        self.bad = w / "weather-2026-11-15.tar.gz"
        self.bad.write_bytes(gzip.compress(b"x" * 5000)[:100])
        os.symlink("/etc/passwd", w / "zzz-link.tar.gz")         # ссылки помощник не трогает
        watch = base / "backup-watch"
        watch.write_text(f"{w} 26\n")
        self.h.OWN_BACKUPS, self.h.WATCH_FILE = str(own), str(watch)
        self.h.OFFSITE_DIR = str(base / "offsite")
        self.h.OFFSITE_STATUS = str(base / "offsite" / "status.json")

    def run_cmd(self, *argv):
        import io
        out = io.BytesIO()
        fake = NS(buffer=out, write=lambda s: out.write(s.encode()), flush=lambda: None)
        with mock.patch.object(sys, "stdout", fake), mock.patch.dict(os.environ, {"SSH_CONNECTION": "93.184.216.34 5 6 22"}):
            self.h.main(list(argv))
        return out.getvalue()

    def test_newest_copies(self):
        items = self.h.newest_copies()
        self.assertEqual([(i[0], i[2]) for i in items],
                         [("monitor-bot", "monitor-bot-2026-11-15_034500.tar.gz"),
                          ("weather-bot", "weather-2026-11-15.tar.gz")])

    def test_pull_stream(self):
        import hashlib
        import io
        import tarfile
        data = self.run_cmd("pull-stream")
        with tarfile.open(fileobj=io.BytesIO(data)) as t:
            names = t.getnames()
            sums = t.extractfile("SHA256SUMS").read().decode().split("\n")
            files = {n: t.extractfile(n).read() for n in names if n != "SHA256SUMS"}
        self.assertEqual(names, ["monitor-bot/monitor-bot-2026-11-15_034500.tar.gz",
                                 "weather-bot/weather-2026-11-15.tar.gz", "SHA256SUMS"])
        for line in filter(None, sums):
            digest, name = line.split("  ")
            self.assertEqual(hashlib.sha256(files[name]).hexdigest(), digest)
        st = json.loads(Path(self.h.OFFSITE_STATUS).read_text())
        self.assertEqual(len(st["files"]), 2)
        self.assertEqual(st["client"], "93.184.216.34")
        self.assertEqual(os.stat(self.h.OFFSITE_STATUS).st_mode & 0o777, 0o644)

    def test_backup_verify(self):
        res = json.loads(self.run_cmd("backup-verify"))
        self.assertEqual([(r["label"], r["ok"]) for r in res], [("monitor-bot", True), ("weather-bot", False)])
        self.assertEqual(res[0]["members"], 1)
        self.assertTrue(res[1]["error"])

    def test_pull_stream_no_backups(self):
        self.h.OWN_BACKUPS, self.h.WATCH_FILE = "/nonexistent", "/nonexistent"
        with self.assertRaises(SystemExit):
            self.run_cmd("pull-stream")
        self.assertFalse(os.path.exists(self.h.OFFSITE_STATUS))


class OffsiteButtonsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        sys.argv = ["bot.py", str(CONF)]
        import bot
        cls.bot = bot

    def test_backup_view_and_buttons(self):
        b = self.bot
        b.S.reset()
        b.STATE.verify_last = {}
        text, markup = b.backup_view()
        self.assertIn("ещё не проверялись", text)
        self.assertIn("ещё ни разу", text)
        cbs = [x.callback_data for row in markup.inline_keyboard for x in row]
        self.assertIn("bk:verify", cbs)
        self.assertIn("bk:pc:168", cbs)
        self.assertTrue(all(len(c.encode()) <= 64 for c in cbs))
        press = BotButtonsTest.press.__get__(self)
        press("bk:pc:168")
        self.assertEqual(b.CFG["offsite_max_age_h"], 168)
        press("bk:pc:off")
        self.assertIsNone(b.CFG["offsite_max_age_h"])
        press("bk:vauto")
        self.assertFalse(b.CFG["verify_backups"])
        res = [{"path": "бот", "name": "a.tar.gz", "ok": True, "members": 3, "size": 10}]
        with mock.patch.object(b.backups, "verify_all", return_value=(res, None)):
            q, upd = press("bk:verify")
        self.assertIn("читается целиком", upd.effective_message.reply_text.call_args[0][0])
        self.assertIn("все копии читаются", b.verify_line())
        b.S.reset()
        b.STATE.verify_last = {}


class IncidentsTest(unittest.TestCase):
    """Этап 15: журнал инцидентов и недельный отчёт."""

    def setUp(self):
        import incidents
        self.m = incidents
        self.inc = incidents.Incidents(Path(tempfile.mkdtemp()) / "metrics.db")
        self.cfg = {"thresholds": {"duration_sec": 300}, "remind_min": 0, "tz": "UTC",
                    "quiet_hours": {"start": "00:00", "end": "23:59"}}

    def run_step(self, st, now, res):
        with mock.patch.object(checks, "oom_kills", return_value=None):
            return checks.step(self.cfg, st, now, res, None, self.inc)

    def test_step_records_even_in_quiet_hours(self):
        st = checks.AlertState()
        self.run_step(st, T0, results(cpu="CPU 99%"))
        self.assertEqual(self.inc.recent(), [])                  # ещё не дольше порога
        out = self.run_step(st, T0 + 400, results(cpu="CPU 99%"))
        self.assertEqual(out, [])                                # тихие часы: cpu не критичен, молчим
        rows = self.inc.recent()
        self.assertEqual([(r[0], r[2], r[3]) for r in rows], [("cpu", T0, None)])  # но в журнал попал
        self.run_step(st, T0 + 500, results(cpu="CPU 99%"))
        self.assertEqual(len(self.inc.recent()), 1)              # не дублируется
        self.run_step(st, T0 + 900, results())
        self.assertEqual(self.inc.recent()[0][3], T0 + 900)
        self.assertEqual(self.inc.open_keys(), set())

    def test_removed_key_closed_and_events(self):
        st = checks.AlertState()
        self.inc.open("svc:old", "служба old", T0)
        self.run_step(st, T0 + 60, results())
        self.assertEqual(self.inc.recent()[0][3], T0 + 60)       # ключ убрали из настроек — закрываем
        reopened = self.m.Incidents(self.inc.path)
        self.assertEqual(reopened.open_keys(), set())
        self.inc.event("log:w", "🔁 Контейнер w перезапустился", T0)
        self.inc.event(None, "💥 OOM killer убил процесс(ы): 1", T0 + 1)
        ev = self.inc.between(T0 - 10, T0 + 10, "event")
        self.assertEqual([self.m.event_kind(e[1]) for e in ev], ["перезапуски контейнеров", "OOM killer"])

    def test_open_survives_restart(self):
        self.inc.open("ctr:w", "контейнер w", T0)
        again = self.m.Incidents(self.inc.path)
        self.assertEqual(again.open_keys(), {"ctr:w"})
        again.open("ctr:w", "контейнер w", T0 + 5)
        self.assertEqual(len(again.recent()), 1)

    def test_incidents_text(self):
        self.assertIn("Пока пусто", self.m.incidents_text(self.inc, self.cfg, T0))
        self.inc.open("web:https://site.example", "не отвечает", T0 - 3 * 3600)
        self.inc.close("web:https://site.example", T0 - 3 * 3600 + 600)
        self.inc.open("ctr:weather-bot", "лежит", T0 - 1800)
        text = self.m.incidents_text(self.inc, self.cfg, T0)
        self.assertIn("🔴 weather-bot: с", text)
        self.assertIn("https://site.example: 10 мин", text)
        self.assertIn("За 7 дней: 2 инцидент(ов), простоя в сумме 40 мин", text)

    def test_weekly_text(self):
        cfg = dict(self.cfg, probes=["https://site.example", "tcp://db.example:5432"])
        self.inc.open("web:https://site.example", "x", T0 - 2 * 86400)
        self.inc.close("web:https://site.example", T0 - 2 * 86400 + 3024)  # 50,4 мин = 0,5% недели
        self.inc.open("cpu", "x", T0 - 10 * 86400)                          # прошлая неделя
        self.inc.close("cpu", T0 - 10 * 86400 + 600)
        self.inc.event(None, "🛡 Подбор пароля по SSH", T0 - 100)
        h = filled_history(TMP / "weekly.db")
        text = self.m.weekly_text(cfg, self.inc, h, T0, ["💾 Бэкапы: ok"])
        self.assertIn("Инцидентов: 1 (неделей раньше 1)", text)
        self.assertIn("https://site.example: 99.50%", text)
        self.assertIn("tcp://db.example:5432: 100%", text)
        self.assertIn("CPU в среднем", text)
        self.assertIn("подбор пароля по SSH — 1", text)
        self.assertTrue(text.endswith("💾 Бэкапы: ok"))
        empty = self.m.weekly_text(self.cfg, self.m.Incidents(Path(tempfile.mkdtemp()) / "m.db"), None, T0)
        self.assertIn("✅ Инцидентов не было", empty)


class WeeklyButtonsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        sys.argv = ["bot.py", str(CONF)]
        import bot
        cls.bot = bot

    def test_buttons(self):
        b = self.bot
        b.S.reset()
        press = BotButtonsTest.press.__get__(self)
        text, markup = b.summary_view()
        cbs = [x.callback_data for row in markup.inline_keyboard for x in row]
        self.assertIn("wk:now", cbs)
        self.assertIn("• 09:00", [x.text for row in markup.inline_keyboard for x in row])  # по умолчанию включён
        press("wk:time:off")
        self.assertIsNone(b.CFG["weekly_report"])
        press("wk:time:20:00")
        self.assertEqual(b.CFG["weekly_report"], "20:00")
        q, upd = press("inc")
        self.assertIn("Журнал инцидентов", upd.effective_message.reply_text.call_args[0][0])
        with mock.patch.object(b.ACT, "_helper", return_value=(True, '["185.220.101.5"]')), \
             mock.patch.object(b.history, "chart_png", return_value=b"png"):
            press("wk:now")
        self.ctx.bot.send_message.assert_called()
        sent = self.ctx.bot.send_message.call_args[0][1]
        self.assertIn("📅 <b>Неделя", sent)
        self.assertIn("Заблокировано IP ботом: 1", sent)
        self.ctx.bot.send_photo.assert_called()
        self.assertIn(("incidents", "📒 Журнал инцидентов"), b.COMMANDS)
        b.S.reset()


class AutobanTest(unittest.TestCase):
    """Этап 14: автобан при подборе пароля."""

    def state(self, *fails):
        st = checks.AlertState()
        st.ssh_failed = [[T0 - 60, ip, n] for ip, n in fails]
        return st

    def test_candidates(self):
        import security
        cfg = {"autoban": True, "autoban_threshold": 10, "autoban_whitelist": ["45.155.205.9"]}
        st = self.state(("185.220.101.5", 7), ("185.220.101.5", 5), ("91.240.118.7", 3),
                        ("80.94.95.3", 50), ("45.155.205.9", 40), ("10.0.0.5", 99))
        st.known_ips = ["80.94.95.3"]                       # с него уже входили — не трогаем
        self.assertEqual(security.autoban_candidates(cfg, st, T0), [("185.220.101.5", 12)])
        st.ssh_failed.append([T0 - 4000, "93.184.216.34", 100])  # старше часа
        self.assertEqual(len(security.autoban_candidates(cfg, st, T0)), 1)
        st.autoban_skip = {"185.220.101.5": T0 - 60}         # помощник недавно отказал
        self.assertEqual(security.autoban_candidates(cfg, st, T0), [])
        st.autoban_skip = {}
        st.autobans = {"185.220.101.5": T0 + 100}            # уже заблокирован
        self.assertEqual(security.autoban_candidates(cfg, st, T0), [])
        st.autobans = {}
        self.assertEqual(security.autoban_candidates(dict(cfg, autoban=False), st, T0), [])
        st.autobans = {"1.1.1.1": T0 - 1, "2.2.2.2": 0, "3.3.3.3": T0 + 10}
        self.assertEqual(security.expired_autobans(st, T0), ["1.1.1.1"])
        self.assertEqual(security.ban_duration_text(0), "навсегда")
        self.assertEqual(security.ban_duration_text(24), "на 24 ч")
        self.assertEqual(security.ban_duration_text(168), "на 7 дн.")


class AutobanBotTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        sys.argv = ["bot.py", str(CONF)]
        import bot
        cls.bot = bot

    def setUp(self):
        b = self.bot
        b.S.reset()
        self.saved = (b.STATE.ssh_failed, b.STATE.known_ips, b.STATE.autobans, b.STATE.autoban_skip,
                      b.STATE.ban_history, b.STATE.fail_ips)
        (b.STATE.ssh_failed, b.STATE.known_ips, b.STATE.autobans, b.STATE.autoban_skip,
         b.STATE.ban_history, b.STATE.fail_ips) = [], [], {}, {}, {}, {}

    def tearDown(self):
        b = self.bot
        (b.STATE.ssh_failed, b.STATE.known_ips, b.STATE.autobans, b.STATE.autoban_skip,
         b.STATE.ban_history, b.STATE.fail_ips) = self.saved
        b.S.reset()

    def test_slow_bruteforce_subnet_and_repeat(self):
        """Часть 4: медленный подбор (за час), подсеть /24, повторный бан — навсегда."""
        b = self.bot
        now = time.time()
        # 195.178.110.217: по 1 попытке в минуту — за 10 минут мало, за час 40
        b.STATE.ssh_failed = [[now - 60 * i, "195.178.110.217", 1] for i in range(40)]
        b.STATE.fail_ips = {"195.178.110.26": [110, now - 3000]}          # вчерашний сосед по подсети
        calls = []

        def helper(cmd, target, timeout=0):
            calls.append((cmd, target))
            return True, f"banned {target}"
        with mock.patch.object(b.ACT, "_helper", side_effect=helper):
            banned, _ = b.autoban_step(now)
        self.assertEqual(banned, [("195.178.110.217", 40), ("195.178.110.0/24", 2)])
        self.assertEqual(calls[-1], ("ban-ip", "195.178.110.0/24"))
        self.assertIn("подсеть 195.178.110.0/24", b.autoban_line("195.178.110.0/24", 2))
        self.assertIn("за час", b.autoban_line("195.178.110.217", 40))
        # подсеть уже в бане: отдельные IP из неё больше не баним
        b.STATE.ssh_failed = [[now - 30, "195.178.110.5", 30]]
        with mock.patch.object(b.ACT, "_helper", side_effect=helper):
            self.assertEqual(b.autoban_step(now + 60), ([], []))
        # бан истёк, подборщик вернулся — второй раз навсегда
        b.STATE.autobans = {}
        b.STATE.ssh_failed = [[now + 100, "91.240.118.7", 15]]
        b.STATE.ban_history["91.240.118.7"] = [1, now - 86400]
        with mock.patch.object(b.ACT, "_helper", side_effect=helper):
            b.autoban_step(now + 120)
        self.assertEqual(b.STATE.autobans["91.240.118.7"], 0)
        self.assertIn("навсегда (уже банили)", b.autoban_line("91.240.118.7", 15))

    def test_step(self):
        b = self.bot
        now = time.time()
        b.STATE.ssh_failed = [[now - 30, "185.220.101.5", 15], [now - 30, "91.240.118.7", 12],
                              [now - 30, "80.94.95.3", 11]]
        b.STATE.autobans = {"45.155.205.9": now - 5}

        def helper(cmd, ip, timeout=0):
            return {"185.220.101.5": (True, "banned 185.220.101.5"),
                    "91.240.118.7": (True, "already banned 91.240.118.7"),
                    "80.94.95.3": (False, "refused: this IP has an open SSH session"),
                    "45.155.205.9": (True, "unbanned 45.155.205.9")}[ip]
        with mock.patch.object(b.ACT, "_helper", side_effect=helper) as hp:
            banned, lifted = b.autoban_step(now)
        self.assertEqual(banned, [("185.220.101.5", 15)])
        self.assertEqual(lifted, ["45.155.205.9"])
        self.assertAlmostEqual(b.STATE.autobans["185.220.101.5"], now + 24 * 3600)
        self.assertNotIn("91.240.118.7", b.STATE.autobans)      # ручной бан остаётся постоянным
        self.assertIn("80.94.95.3", b.STATE.autoban_skip)
        self.assertEqual([c.args[0] for c in hp.call_args_list].count("ban-ip"), 3)
        with mock.patch.object(b.ACT, "_helper", side_effect=helper) as hp:
            self.assertEqual(b.autoban_step(now + 60), ([], []))  # повторно не трогаем
        hp.assert_not_called()

    def test_forever_and_buttons(self):
        b = self.bot
        now = time.time()
        b.S.set("autoban_hours", 0)
        b.STATE.ssh_failed = [[now - 30, "185.220.101.5", 15]]
        with mock.patch.object(b.ACT, "_helper", return_value=(True, "banned")):
            b.autoban_step(now)
        self.assertEqual(b.STATE.autobans["185.220.101.5"], 0)
        text, markup = b.security_view()
        self.assertIn("навсегда", text)
        cbs = [x.callback_data for row in markup.inline_keyboard for x in row]
        for c in ("sec:autoban", "abthr:5", "abh:168", "abh:0"):
            self.assertIn(c, cbs)
        press = BotButtonsTest.press.__get__(self)
        press("abthr:20")
        self.assertEqual(b.CFG["autoban_threshold"], 20)
        press("sec:autoban")
        self.assertFalse(b.CFG["autoban"])
        self.assertIn("Автобан: выкл", b.security_view()[0])


class MaintenanceTest(unittest.TestCase):
    """Этап 17: обслуживание и тихие часы 23:00–06:00."""

    def test_step_silent_and_planned(self):
        import incidents
        inc = incidents.Incidents(Path(tempfile.mkdtemp()) / "m.db")
        cfg = {"thresholds": {"duration_sec": 0}, "remind_min": 0, "tz": "UTC"}
        st = checks.AlertState()
        st.maint_until = T0 + 1800
        with mock.patch.object(checks, "oom_kills", return_value=None):
            out = checks.step(cfg, st, T0, results(**{"svc:ssh": "служба ssh не работает"}), None, inc)
            self.assertEqual(out, [])                            # даже критичный алерт молчит
            self.assertTrue(inc.recent()[0][1].startswith(checks.MAINT_MARK))
            out = checks.step(cfg, st, T0 + 1900, results(**{"svc:ssh": "служба ssh не работает"}), None, inc)
        self.assertEqual([a.kind for a in out], ["alert"])       # после обслуживания алерт приходит
        inc.close("svc:ssh", T0 + 2000)
        inc.open("cpu", "CPU 99%", T0 + 2100)
        inc.close("cpu", T0 + 2700)
        text = incidents.weekly_text(cfg, inc, None, T0 + 3000)
        self.assertIn("Инцидентов: 1 ", text)                    # плановый не считается
        self.assertIn("🔧 Во время обслуживания: 1", text)
        self.assertIn("(обслуживание)", incidents.incidents_text(inc, cfg, T0 + 3000))
        self.assertIn("простоя в сумме 10 мин", incidents.incidents_text(inc, cfg, T0 + 3000))

    def test_quiet_23_06(self):
        cfg = {"tz": "UTC", "quiet_hours": {"start": "23:00", "end": "06:00"}}
        from datetime import datetime, timezone
        ts = lambda h: datetime(2026, 11, 15, h, 30, tzinfo=timezone.utc).timestamp()
        self.assertTrue(checks.in_quiet_hours(cfg, ts(23)))
        self.assertTrue(checks.in_quiet_hours(cfg, ts(5)))
        self.assertFalse(checks.in_quiet_hours(cfg, ts(6)))


class MaintenanceBotTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        sys.argv = ["bot.py", str(CONF)]
        import bot
        cls.bot = bot

    def test_buttons(self):
        b = self.bot
        b.S.reset()
        _, markup = b.quiet_view()
        self.assertIn("quiet:23:00-06:00", [x.callback_data for row in markup.inline_keyboard for x in row])
        press = BotButtonsTest.press.__get__(self)
        press("quiet:23:00-06:00")
        self.assertEqual(b.CFG["quiet_hours"], {"start": "23:00", "end": "06:00"})
        cbs = [x.callback_data for row in b.alerts_kb().inline_keyboard for x in row]
        self.assertIn("mt:1800", cbs)
        press("mt:3600")
        self.assertTrue(b.STATE.in_maintenance(time.time()))
        self.assertIn("Идёт обслуживание", b.alerts_text())
        self.assertIn("mt:off", [x.callback_data for row in b.alerts_kb().inline_keyboard for x in row])
        press("mt:off")
        self.assertFalse(b.STATE.in_maintenance(time.time()))
        saved = b.STATE.since
        b.STATE.since = {"ctr:weather-bot": time.time()}
        self.assertIn("weather-bot", b.maint_end_text())
        b.STATE.since = saved
        self.assertIn("maint", [c for c, _ in b.COMMANDS])
        b.S.reset()


# ---------- исправления по ревью 2026-11-15 ----------

class ReviewFixesHelperTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.h = load_helper()

    def code(self, *argv):
        with mock.patch("builtins.print"), self.assertRaises(SystemExit) as e:
            self.h.main(list(argv))
        return e.exception.code

    def test_docker_formats_match_bot(self):
        self.assertEqual(self.h.PS_FMT, checks.PS_FMT)
        self.assertEqual(self.h.INSPECT_FMT, checks.INSPECT_FMT)
        self.assertEqual(self.h.STATS_FMT, checks.STATS_FMT)

    def test_docker_allowlist(self):
        h = self.h
        with mock.patch.object(h.subprocess, "run") as run:
            for bad in (["run", "-v", "/:/host", "alpine"], ["ps", "-a", "--format", "{{json .}}"],
                        ["inspect", "--format", "{{.Config.Env}}", "w"], ["logs", "--tail", "30", "-f"],
                        ["logs", "--tail", "99999", "w"], ["restart", "--time=0"], ["exec", "w", "sh"],
                        ["inspect", "--format", h.INSPECT_FMT, "w;rm"]):
                self.assertEqual(self.code("docker", *bad), 2, bad)
            run.assert_not_called()
            run.return_value = mock.Mock(returncode=0, stdout=b"w\trunning\n", stderr=b"")
            with mock.patch.object(h.sys, "stdout", mock.Mock()), mock.patch.object(h.sys, "stderr", mock.Mock()):
                for good in (["ps", "-a", "--format", h.PS_FMT], ["stats", "--no-stream", "--format", h.STATS_FMT],
                             ["inspect", "--format", h.INSPECT_FMT, "weather-bot", "x"],
                             ["logs", "--tail", "30", "weather-bot"], ["logs", "--since", "1800000000", "w"],
                             ["restart", "weather-bot"]):
                    self.assertEqual(self.code("docker", *good), 0, good)
                    self.assertEqual(run.call_args.args[0], ["docker", *good])

    def test_kill_protects_packages_and_update(self):
        h = self.h
        child = subprocess.Popen(["sleep", "30"])
        self.addCleanup(child.kill)
        for comm in ("dpkg", "apt-get", "unattended-upgr", "containerd-shim", "systemd-udevd", "sshd-auth"):
            with mock.patch.object(h, "proc_name", return_value=comm), mock.patch.object(h.os, "kill") as k:
                self.assertEqual(self.code("kill", str(child.pid)), 2, comm)
                k.assert_not_called()
        real_open = open

        def fake_open(path, *a, **kw):
            if str(path).endswith("/cgroup"):
                return io.StringIO("0::/system.slice/monitor-bot-update-abcdef1.service\n")
            return real_open(path, *a, **kw)
        with mock.patch("builtins.open", fake_open), mock.patch.object(h.os, "kill") as k:
            self.assertEqual(self.code("kill", str(child.pid)), 2)
            k.assert_not_called()

    def test_check_in_main(self):
        h = self.h
        h.check_in_main("a" * 40, api_fn=lambda p: {"status": "behind"})
        h.check_in_main("a" * 40, api_fn=lambda p: {"status": "identical"})
        for st in ("diverged", "ahead", None):
            with self.assertRaises(RuntimeError):
                h.check_in_main("a" * 40, api_fn=lambda p, st=st: {"status": st})

    def test_update_refuses_commit_outside_main(self):
        h = self.h
        base = Path(tempfile.mkdtemp())
        with mock.patch.object(h, "RESULT_FILE", str(base / "res.json")), \
             mock.patch.object(h, "LOCK_FILE", str(base / "lock")), \
             mock.patch.object(h, "check_ownership"), \
             mock.patch.object(h, "github_api", return_value={"status": "diverged"}), \
             mock.patch.object(h, "download") as dl:
            h.update_run("c" * 40)
        dl.assert_not_called()
        res = json.loads((base / "res.json").read_text())
        self.assertFalse(res["ok"])
        self.assertIn("не входит в ветку main", res["error"])

    def test_unsafe_paths(self):
        d = Path(tempfile.mkdtemp())
        (d / "bin").mkdir()
        (d / "bin" / "pip").write_text("x")
        if os.geteuid() == 0:
            os.chmod(d, 0o755)
            os.chmod(d / "bin", 0o755)
            os.chmod(d / "bin" / "pip", 0o644)
            self.assertEqual(self.h.unsafe_paths(str(d)), [])
            os.chmod(d / "bin" / "pip", 0o666)
            self.assertEqual(self.h.unsafe_paths(str(d)), [str(d / "bin" / "pip")])
        else:
            self.assertIn(str(d), self.h.unsafe_paths(str(d)))  # владелец не root

    def test_pip_only_wheels(self):
        with mock.patch.object(self.h, "run") as run:
            self.h.pip_install("/x/requirements.txt")
        self.assertIn("--only-binary=:all:", run.call_args.args[0])

    def test_copy_code_does_not_follow_planted_symlink(self):
        h = self.h
        base = Path(tempfile.mkdtemp())
        src, dst = base / "src", base / "dst"
        src.mkdir()
        dst.mkdir()
        (src / "bot.py").write_text("new\n")
        target = base / "victim"
        target.write_text("secret\n")
        (dst / "bot.py").symlink_to(target)
        with mock.patch.object(h.os, "chown"):
            h.copy_code(str(src), str(dst), 0)
        self.assertEqual(target.read_text(), "secret\n")
        self.assertFalse((dst / "bot.py").is_symlink())
        self.assertEqual((dst / "bot.py").read_text(), "new\n")

    def test_hardlink_refused_without_protection(self):
        h = self.h
        d = Path(tempfile.mkdtemp())
        (d / "a").write_text("x")
        os.link(d / "a", d / "b")
        with mock.patch.object(h, "hardlinks_protected", return_value=False):
            with self.assertRaises(OSError):
                h.open_regular(str(d / "b"))
        with mock.patch.object(h, "hardlinks_protected", return_value=True):
            h.open_regular(str(d / "b")).close()

    def test_sshd_ports(self):
        h = self.h
        ss = ('LISTEN 0 128 0.0.0.0:2222 0.0.0.0:* users:(("sshd",pid=1,fd=3))\n'
              'LISTEN 0 128 0.0.0.0:80 0.0.0.0:* users:(("nginx",pid=2,fd=3))\n')

        def run(cmd, timeout=300):
            if cmd[0] == "ss" and "-tlnp" in cmd:
                return mock.Mock(returncode=0, stdout=ss, stderr="")
            if cmd[0] == "ss":
                return mock.Mock(returncode=0, stdout="0 0 5.1.1.1:2222 91.240.118.7:5000\n"
                                                      "0 0 5.1.1.1:80 80.94.95.3:4000\n", stderr="")
            return mock.Mock(returncode=0, stdout="", stderr="")
        with mock.patch.object(h, "run", run):
            self.assertEqual(h.sshd_ports(), {"2222"})
            self.assertEqual(h.ssh_peers(), {"91.240.118.7"})
        sock = mock.Mock(returncode=0, stdout="[::]:2200 (Stream)\n", stderr="")
        with mock.patch.object(h, "run", lambda cmd, timeout=300: sock if cmd[0] == "systemctl"
                               else mock.Mock(returncode=0, stdout="", stderr="")):
            self.assertEqual(h.sshd_ports(), {"2200"})


class ReviewFixesTest(unittest.TestCase):
    def test_ssh_username_injection(self):
        text = ("Invalid user x from 8.8.8.8 port 1 from 203.0.113.9 port 51000\n"
                "Failed password for invalid user x from 8.8.8.8 port 1 from 203.0.113.9 port 51000 ssh2\n")
        _, failed = security.parse_ssh(text)
        self.assertEqual(failed, {"203.0.113.9": 1})

    def test_ssh_invalid_user_counted_once(self):
        text = ("Invalid user bob from 1.2.3.4 port 5\n"
                "Failed password for invalid user bob from 1.2.3.4 port 5 ssh2\n"
                "Failed password for invalid user bob from 1.2.3.4 port 5 ssh2\n"
                "Invalid user eve from 1.2.3.4 port 6\n")
        _, failed = security.parse_ssh(text)
        self.assertEqual(failed, {"1.2.3.4": 3})   # два пароля в одном соединении + одно без пароля

    def test_ssh_read_while_muted(self):
        cfg = {"ssh_alerts": True, "port_alerts": False, "update_alerts": False}
        st = checks.AlertState(ssh_checked=T0)
        text = ("Failed password for root from 1.1.1.1 port 1 ssh2\n"
                "Accepted publickey for root from 203.0.113.5 port 2 ssh2: ED25519 SHA256:x\n")
        out = security.events(cfg, st, T0 + 60, True, False, ssh_text_fn=lambda since: text)
        self.assertEqual(out, [])                         # молчим
        self.assertEqual(st.ssh_failed[0][1:], ["1.1.1.1", 1])  # но попытки посчитаны (автобан работает)
        self.assertIn("203.0.113.5", st.known_ips)

    def test_known_ips_expire_for_autoban(self):
        st = checks.AlertState(known_ips=["80.94.95.3"], known_ip_at={"80.94.95.3": T0},
                               ssh_failed=[[T0 + 40 * 86400, "80.94.95.3", 50]])
        cfg = {"autoban_threshold": 10}
        self.assertEqual(security.autoban_candidates(cfg, st, T0 + 40 * 86400 + 10), [("80.94.95.3", 50)])
        st.known_ip_at["80.94.95.3"] = T0 + 39 * 86400
        self.assertEqual(security.autoban_candidates(cfg, st, T0 + 40 * 86400 + 10), [])

    def test_corrupt_state_kept_and_reported(self):
        p = TMP / "state_corrupt.json"
        p.write_text("{not json", encoding="utf-8")
        st = checks.AlertState.load(p)
        self.assertIn("повреждён", st.load_error)
        self.assertTrue((TMP / "state_corrupt.json.bad").exists())
        st.save()
        self.assertNotIn("load_error", json.loads(p.read_text()))
        self.assertEqual(checks.AlertState.load(p).load_error, "")

    def test_old_state_gets_known_ip_time(self):
        p = TMP / "state_old_ips.json"
        p.write_text(json.dumps({"known_ips": ["1.2.3.4"]}), encoding="utf-8")
        st = checks.AlertState.load(p)
        self.assertIn("1.2.3.4", st.known_ip_at)

    def test_save_from_threads(self):
        import threading
        st = checks.AlertState.load(TMP / "state_threads.json")
        errors = []

        def work(i):
            try:
                for j in range(30):
                    st.since[f"k{i}-{j}"] = j
                    st.save()
            except Exception as e:  # noqa: BLE001
                errors.append(e)
        ts = [threading.Thread(target=work, args=(i,)) for i in range(4)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertEqual(errors, [])
        self.assertEqual(len(json.loads((TMP / "state_threads.json").read_text())["since"]), 120)

    def test_seen_containers_forgotten_after_a_day(self):
        st = checks.AlertState(seen_containers=["keep", "tmp", "fixed"])
        cfg = {"containers": ["fixed"]}
        with mock.patch.object(checks, "container_states", return_value={"keep": "exited"}):
            self.assertEqual(checks.update_seen_containers(st, cfg, T0), [])
            self.assertEqual(checks.update_seen_containers(st, cfg, T0 + 86400), ["tmp"])
        self.assertEqual(st.seen_containers, ["keep", "fixed"])
        with mock.patch.object(checks, "container_states", return_value=None):  # docker недоступен
            self.assertEqual(checks.update_seen_containers(st, cfg, T0 + 10 * 86400), [])
        self.assertEqual(st.seen_containers, ["keep", "fixed"])

    def test_container_name_validation(self):
        self.assertTrue(checks.valid_container("weather-bot"))
        for bad in ("-f", "--follow", "", "a b", "a;b", "x" * 200, "имя"):
            self.assertFalse(checks.valid_container(bad), bad)
        with mock.patch.object(checks, "docker") as d:
            self.assertIsNone(checks.container_logs("--follow"))
            d.assert_not_called()

    def test_docker_via_helper_and_fallback(self):
        checks._docker_mode["direct_until"] = 0.0
        self.addCleanup(checks._docker_mode.update, {"direct_until": 0.0})
        calls = []

        def run(cmd, **kw):
            calls.append(cmd)
            if cmd[0] == "sudo":
                return mock.Mock(returncode=2, stdout="unknown command\n", stderr="")
            return mock.Mock(returncode=0, stdout="w\trunning\n", stderr="")
        with mock.patch.object(checks, "docker_via_helper", side_effect=[True, False]), \
             mock.patch.object(checks.shutil, "which", return_value="/usr/bin/docker"), \
             mock.patch.object(checks.subprocess, "run", run):
            self.assertEqual(checks.container_states(), {"w": "running"})
        self.assertEqual(calls[0][:4], ["sudo", "-n", checks.HELPER, "docker"])
        self.assertEqual(calls[1][0], "docker")              # старый помощник: напрямую
        self.assertGreater(checks._docker_mode["direct_until"], time.time())

    def test_helper_repo(self):
        f = TMP / "update-repo"
        f.write_text("someone/fork\n")
        self.assertEqual(updater.helper_repo(f), "someone/fork")
        f.write_text("bad repo; rm\n")
        self.assertIsNone(updater.helper_repo(f))
        self.assertIsNone(updater.helper_repo(TMP / "no-such-file"))


class ReviewFixesBotTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        sys.argv = ["bot.py", str(CONF)]
        import bot
        cls.bot = bot

    def test_parse_duration_overflow(self):
        self.assertIsNone(self.bot.parse_duration("1e400d"))
        self.assertEqual(self.bot.parse_duration("2h"), 7200)

    def test_allowed_in_group(self):
        b = self.bot
        upd = mock.MagicMock()
        upd.effective_chat.id = 42
        upd.effective_chat.type = "private"
        self.assertTrue(b.allowed(upd))
        upd.effective_chat.id = 7
        self.assertFalse(b.allowed(upd))
        b.CFG["allowed_chats"] = [42, -100500]
        try:
            upd.effective_chat.id = -100500
            upd.effective_chat.type = "supergroup"
            upd.effective_user.id = 999
            self.assertFalse(b.allowed(upd))     # чужой участник группы
            upd.effective_user.id = 42
            self.assertTrue(b.allowed(upd))
        finally:
            b.CFG["allowed_chats"] = [42]

    def test_long_name_button_not_truncated(self):
        btn = self.bot.toggle_btn("x", "services", "a" * 80)
        self.assertEqual(btn.callback_data, "noop")
        self.assertEqual(self.bot.toggle_btn("x", "services", "cron").callback_data, "tg:services:cron")

    def test_failed_unban_is_retried_not_forgotten(self):
        b = self.bot
        saved = (dict(b.STATE.autobans), b.STATE.ssh_failed)
        self.addCleanup(lambda: (setattr(b.STATE, "autobans", saved[0]), setattr(b.STATE, "ssh_failed", saved[1])))
        now = time.time()
        b.STATE.autobans = {"45.155.205.9": now - 5}
        b.STATE.ssh_failed = []
        with mock.patch.object(b.ACT, "_helper", return_value=(False, "ufw failed")):
            banned, lifted = b.autoban_step(now)
        self.assertEqual(lifted, [])
        self.assertGreater(b.STATE.autobans["45.155.205.9"], now)   # повторим позже
        with mock.patch.object(b.ACT, "_helper", return_value=(False, "этот IP блокировал не бот")):
            b.autoban_step(now + 4000)
        self.assertNotIn("45.155.205.9", b.STATE.autobans)          # правила уже нет — забываем


class Part4Test(unittest.TestCase):
    """Часть 4: меньше шума от деплоя, перезагрузки и сбоев связи; честный недельный отчёт."""
    DEPLOY_FP = "SHA256:RRBViC4+Wqsi4RUOvklwpOd/6ztXeUWJoabjbmasyls"

    def test_deploy_login_and_restart_are_journal_only(self):
        cfg = {"ssh_alerts": True, "port_alerts": False, "update_alerts": False, "deploy_keys": [self.DEPLOY_FP],
               "containers": ["weather-bot"], "log_alerts": False}
        st = checks.AlertState(ssh_checked=T0, ctr_started={"weather-bot": "2026-11-15T13:12:00Z"})
        text = f"Accepted publickey for root from 172.200.165.147 port 61569 ssh2: ED25519 {self.DEPLOY_FP}\n"
        self.assertEqual(security.events(cfg, st, T0 + 60, False, False, ssh_text_fn=lambda s: text), [])
        self.assertNotIn("172.200.165.147", st.known_ips)       # не «новый IP» и не копится в своих
        self.assertEqual(st.deploy_at, T0 + 60)
        info = {"weather-bot": {"restarts": 0, "started": "2026-11-15T17:19:51.273824495Z", "health": ""}}
        out = checks.docker_events(cfg, st, T0 + 70, set(), False, False, info)
        self.assertEqual([(a.text[:8], a.send) for a in out], [("🚀 Деплой", "journal")])
        # без деплоя тот же перезапуск — обычный алерт
        info["weather-bot"]["started"] = "2026-11-15T18:00:00Z"
        out = checks.docker_events(cfg, st, T0 + 60 + checks.DEPLOY_WINDOW + 1, set(), False, False, info)
        self.assertEqual([a.send for a in out], ["loud"])
        self.assertIn("перезапустился", out[0].text)
        # чужой ключ — обычный вход с нового IP
        other = "Accepted publickey for root from 9.9.9.9 port 1 ssh2: ED25519 SHA256:other\n"
        st.ssh_checked = T0
        out = security.events(cfg, st, T0 + 5000, False, False, ssh_text_fn=lambda s: other)
        self.assertTrue(any("нового IP 9.9.9.9" in a.text for a in out))
        self.assertTrue(security.is_deploy({"deploy_nets": ["20.0.0.0/8"]}, "20.171.127.98", ""))

    def test_container_started_with_server_boot_is_silent(self):
        cfg = {"containers": ["w"], "log_alerts": False}
        boot = checks.docker_time("2026-11-15T12:47:00Z")
        st = checks.AlertState(boot_time=boot, ctr_started={"w": "2026-11-15T10:00:00Z"})
        info = {"w": {"restarts": 0, "started": "2026-11-15T12:47:40.5Z", "health": ""}}
        self.assertEqual(checks.docker_events(cfg, st, boot + 100, set(), False, False, info), [])
        self.assertEqual(st.ctr_started["w"], "2026-11-15T12:47:40.5Z")
        self.assertAlmostEqual(checks.docker_time("2026-11-15T12:47:40.5Z") - boot, 40.5)
        self.assertIsNone(checks.docker_time("t1"))

    def test_network_errors_in_logs_are_quiet(self):
        cfg = {"containers": ["w"], "log_check_min": 1}
        st = checks.AlertState()
        info = {"w": {"restarts": 0, "started": "s", "health": ""}}
        net = "ERROR:root:ошибка рассылки\nTraceback (most recent call last):\ntelegram.error.TimedOut: Timed out\n"
        checks.docker_events(cfg, st, T0, set(), False, False, info, lambda n, s: "")
        out = checks.docker_events(cfg, st, T0 + 60, set(), False, False, info, lambda n, s: net)
        self.assertEqual(len(out), 1)
        self.assertTrue(out[0].text.startswith("🌐 Сетевые ошибки в логах w"))
        self.assertEqual(out[0].send, "quiet")
        # бот сам только что терял связь — ошибки рядом по времени тоже считаем сбоем связи
        st.net_fail_at = T0 + 1000
        out = checks.docker_events(cfg, st, T0 + 1100, set(), False, False, info, lambda n, s: "ERROR x")
        self.assertIn("связь пропадала", out[0].text)
        st.net_fail_at = 0
        out = checks.docker_events(cfg, st, T0 + 2200, set(), False, False, info, lambda n, s: "ERROR x")
        self.assertTrue(out[0].text.startswith("⚠️ Ошибки в логах"))
        import incidents
        self.assertEqual(incidents.event_kind("🌐 Сетевые ошибки"), "сбои связи")

    def test_logs_checked_every_5_min_by_default(self):
        cfg = {"containers": ["w"]}
        st = checks.AlertState()
        info = {"w": {"restarts": 0, "started": "s", "health": ""}}
        seen = []

        def logs(n, since):
            seen.append(since)
            return ""
        for i in range(11):
            checks.docker_events(cfg, st, T0 + 60 * i, set(), False, False, info, logs)
        self.assertEqual(seen, [T0, T0 + 300])                   # с первого просмотра ничего не теряется

    def test_docker_poll_cache(self):
        self.addCleanup(checks._poll.update, {"ts": 0.0, "unsupported_until": 0.0})
        payload = json.dumps({"ps": "w\trunning\nx\texited\n",
                              "inspect": "/w\t1\t2026-11-15T07:00:00Z\thealthy\t\n/x\t0\t2026-11-14T07:00:00Z\t\t\n"})
        calls = []

        def run(cmd, **kw):
            calls.append(cmd)
            return mock.Mock(returncode=0, stdout=payload, stderr="")
        with mock.patch.object(checks, "docker_via_helper", return_value=True), \
             mock.patch.object(checks.subprocess, "run", run):
            self.assertTrue(checks.docker_poll())
            self.assertEqual(checks.container_states(), {"w": "running", "x": "exited"})
            self.assertEqual(checks.container_info(["w", "nope"])["w"]["restarts"], 1)
            self.assertNotIn("nope", checks.container_info(["w", "nope"]))
        self.assertEqual(calls, [["sudo", "-n", checks.HELPER, "docker-poll"]])   # один вызов на всё
        # старый помощник без docker-poll: обычные вызовы, повтор не раньше чем через 10 минут
        with mock.patch.object(checks, "docker_via_helper", return_value=True), \
             mock.patch.object(checks.subprocess, "run",
                               return_value=mock.Mock(returncode=2, stdout="unknown command\n", stderr="")):
            self.assertFalse(checks.docker_poll())
        self.assertGreater(checks._poll["unsupported_until"], time.time())
        self.assertIsNone(checks._poll_fresh())

    def test_helper_net_ban_and_poll(self):
        h = load_helper()
        self.assertEqual(str(h.parse_ip("195.178.110.0/24")), "195.178.110.0/24")
        for bad in ("195.178.0.0/16", "195.178.110.1/24", "10.0.0.0/24", "2001:db8::/64"):
            with self.assertRaises(SystemExit):
                with mock.patch("sys.stdout", io.StringIO()):
                    h.parse_ip(bad)
        net = h.parse_ip("195.178.110.0/24")
        self.assertTrue(h.covers_peer(net, {"195.178.110.9"}))
        self.assertFalse(h.covers_peer(net, {"9.246.153.109"}))
        self.assertTrue(h.covers_peer(h.parse_ip("8.8.8.8"), {"8.8.8.8"}))
        ps = mock.Mock(returncode=0, stdout="w\trunning\nbad name\texited\n", stderr="")
        insp = mock.Mock(returncode=0, stdout="/w\t0\tx\t\t\n", stderr="")
        out = io.StringIO()
        with mock.patch.object(h.subprocess, "run", side_effect=[ps, insp]) as run, mock.patch("sys.stdout", out):
            h.docker_poll()
        self.assertEqual(run.call_args_list[1].args[0], ["docker", "inspect", "--format", h.INSPECT_FMT, "w"])
        self.assertEqual(json.loads(out.getvalue())["inspect"], "/w\t0\tx\t\t\n")

    def test_weekly_partial_period_and_deploys(self):
        import incidents
        inc = incidents.Incidents(Path(tempfile.mkdtemp()) / "m.db")
        h = history.History(Path(tempfile.mkdtemp()) / "h.db")
        h.record(fake_sample(5, 40, 50, 1000, 1000), T0 - 23 * 3600)
        h.record(fake_sample(5, 40, 50, 2000, 2000), T0 - 60)
        inc.event("deploy:w", "🚀 Деплой: w перезапущен с новой версией", T0 - 3600)
        inc.event("autoban", "🚫 Автобан: 1.2.3.4", T0 - 3600)
        cfg = {"tz": "UTC", "probes": ["https://site.example"]}
        inc.open("web:https://site.example", "x", T0 - 4 * 3600)
        inc.close("web:https://site.example", T0 - 4 * 3600 + 828)        # 13,8 мин из 23 ч = 1%
        text = incidents.weekly_text(cfg, inc, h, T0)
        self.assertIn("Данные только с", text)
        self.assertIn("23 ч", text)
        self.assertIn("site.example: 99.00%", text)
        self.assertIn("🗒 События: автобан IP — 1", text)
        self.assertIn("🚀 Плановое: деплои — 1", text)

    def test_ssh_stats_and_week_lines(self):
        st = checks.AlertState(autobans={"195.178.110.0/24": 0})
        security.record_failures(st, {"195.178.110.26": 60, "80.94.92.179": 30}, T0)
        security.record_failures(st, {"195.178.110.217": 40}, T0 + 3600)
        self.assertEqual(st.fail_ips["195.178.110.26"], [60, T0])
        lines = security.ssh_week_lines(st, T0 + 7200)
        self.assertIn("130 попыток с 3 IP", lines[0])
        self.assertIn("195.178.110.0/24 (100)", lines[0])
        self.assertIn("подсетей: 1", lines[1])
        security.record_failures(st, {}, T0 + 2 * 86400)
        self.assertEqual(st.fail_ips, {})                              # старше суток забываются

    def test_backup_folder_grace(self):
        st = checks.AlertState()
        cfg = {"backup_alerts": True, "offsite_max_age_h": None}

        def watched(now):
            return [{"path": "/var/backups/new", "max_age_h": 26, "exists": True, "newest": None}]
        self.assertIsNone(backups.evaluate(cfg, st, T0, None, watched)["bak:/var/backups/new"])
        self.assertIn("нет ни одной",
                      backups.evaluate(cfg, st, T0 + 27 * 3600, None, watched)["bak:/var/backups/new"])

    def test_new_settings_defaults(self):
        s = Settings(CONF)
        self.assertEqual((s.cfg["autoban_hour_threshold"], s.cfg["autoban_net_ips"], s.cfg["log_check_min"]),
                         (20, 2, 5))
        self.assertTrue(s.cfg["autoban_repeat_forever"])
        self.assertEqual(s.cfg["deploy_keys"], [])


if __name__ == "__main__":
    unittest.main()
