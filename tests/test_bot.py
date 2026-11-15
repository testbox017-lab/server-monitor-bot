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
from settings import Settings  # noqa: E402


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
        upd = mock.MagicMock()
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
