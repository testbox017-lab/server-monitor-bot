"""Язык интерфейса: полнота переводов, отсутствие непереведённых строк, переключение из Telegram."""
import ast
import asyncio
import re
import string
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import i18n  # noqa: E402
import locales  # noqa: E402

CYR = re.compile(r"[А-Яа-яЁёІіЇїЄєҐґ]")
SOURCES = sorted(p for p in ROOT.glob("*.py") if p.name != "locales.py")
LOG_NAMES = {"log", "logging", "logger"}
# Кириллица, которая нарочно не переводится: разбор ввода и проверки, а не вывод
ALLOWED = {"ВАШ", "🌐 Язык · Мова · Language"}


def placeholders(s: str) -> list:
    return sorted((f, spec, conv) for _, f, spec, conv in string.Formatter().parse(s) if f is not None)


def tr_calls(tree):
    for n in ast.walk(tree):
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id in ("_", "N_") and n.args:
            yield n


def keys_in_code() -> dict[str, str]:
    keys = {}
    for p in SOURCES:
        for n in tr_calls(ast.parse(p.read_text(encoding="utf-8"))):
            a = n.args[0]
            if isinstance(a, ast.Constant) and isinstance(a.value, str):
                keys.setdefault(a.value, f"{p.name}:{n.lineno}")
    return keys


def untranslated(tree) -> list[tuple[int, str]]:
    """Строки с кириллицей вне _()/N_(), докстрингов и журнала."""
    skip = set()
    for n in ast.walk(tree):
        if isinstance(n, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and n.body:
            first = n.body[0]
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
                skip.add(id(first.value))
        if isinstance(n, ast.Call):
            f = n.func
            is_log = (isinstance(f, ast.Attribute) and (
                (isinstance(f.value, ast.Name) and f.value.id in LOG_NAMES)
                or (isinstance(f.value, ast.Call) and getattr(f.value.func, "attr", "") == "getLogger")))
            if is_log or (isinstance(f, ast.Name) and f.id in ("_", "N_")):
                for sub in ast.walk(n):
                    skip.add(id(sub))
        if isinstance(n, ast.Dict):  # разбор единиц в /mute: «2ч», «1д»
            for k in n.keys:
                if isinstance(k, ast.Constant) and isinstance(k.value, str) and len(k.value) == 1:
                    skip.add(id(k))
    out = []
    for n in ast.walk(tree):
        if id(n) in skip or not isinstance(n, ast.Constant) or not isinstance(n.value, str):
            continue
        if CYR.search(n.value) and n.value not in ALLOWED:
            out.append((n.lineno, n.value))
    return out


class TranslationsTest(unittest.TestCase):
    def test_every_string_translated(self):
        keys = keys_in_code()
        self.assertGreater(len(keys), 400)
        for name, table in (("UK", locales.UK), ("EN", locales.EN)):
            missing = [f"{where}: {k!r}" for k, where in keys.items() if k not in table]
            self.assertEqual(missing, [], f"нет перевода в {name}")

    def test_placeholders_match(self):
        for name, table in (("UK", locales.UK), ("EN", locales.EN)):
            for k, v in table.items():
                self.assertEqual(placeholders(k), placeholders(v), f"{name}: {k!r} -> {v!r}")
                self.assertEqual(k.startswith("\n"), v.startswith("\n"), f"{name}: {k!r}")
                self.assertEqual(k.endswith(" "), v.endswith(" "), f"{name}: {k!r}")

    def test_no_stale_translations(self):
        keys = keys_in_code()
        for name, table in (("UK", locales.UK), ("EN", locales.EN)):
            self.assertEqual([k for k in table if k not in keys], [], f"лишние строки в {name}")

    def test_english_has_no_cyrillic(self):
        bad = [v for v in locales.EN.values() if CYR.search(v)]
        self.assertEqual(bad, [])

    def test_no_untranslated_literals(self):
        found = []
        for p in SOURCES:
            if p.name == "i18n.py":
                continue
            found += [f"{p.name}:{ln}: {s[:60]!r}" for ln, s in untranslated(ast.parse(p.read_text(encoding="utf-8")))]
        self.assertEqual(found, [], "строки для пользователя без _()")

    def test_tr_fallbacks(self):
        self.assertEqual(i18n.tr("нет такой строки", "en"), "нет такой строки")
        self.assertEqual(i18n.tr("🔄 Обновить", "uk"), "🔄 Оновити")
        self.assertEqual(i18n.tr("{n} попыток", "en", n=3), "3 attempts")
        self.assertEqual(i18n.tr("{n} попыток", "ru", n=3), "3 попыток")
        with mock.patch.dict(locales.EN, {"{n} попыток": "{broken} attempts"}):  # испорченный перевод
            self.assertEqual(i18n.tr("{n} попыток", "en", n=3), "3 попыток")
        self.assertEqual(i18n.set_lang("xx"), "ru")
        self.assertEqual(i18n.variants("📊 Статус"), {"📊 Статус", "📊 Status"})


class LanguageOutputTest(unittest.TestCase):
    """Тексты модулей на выбранном языке."""

    def tearDown(self):
        i18n.set_lang("ru")

    def test_weekly_report_and_alerts_in_english(self):
        import checks
        import incidents
        import security
        inc = incidents.Incidents(Path(tempfile.mkdtemp()) / "m.db")
        i18n.set_lang("en")
        cfg = {"tz": "UTC", "thresholds": {"duration_sec": 300}, "remind_min": 0, "probes": ["https://site.example"]}
        st = checks.AlertState()
        t0 = 1_800_000_000
        res = {"cpu": None, "ram": None, "reboot_required": None, "svc:ssh": "❌ Service ssh is not running (dead)"}
        with mock.patch.object(checks, "oom_kills", return_value=None):
            out = checks.step(cfg, st, t0, res, None, inc)  # служба критична: сразу
            checks.step(cfg, st, t0 + 400, res, None, inc)
            rec = checks.step(cfg, st, t0 + 900, dict(res, **{"svc:ssh": None}), None, inc)
        self.assertEqual(out[0].text, "🚨 ❌ Service ssh is not running (dead)")
        self.assertEqual(rec[0].text, "✅ Recovered: ssh")
        inc.event("autoban", "🚫 Autoban: 1.2.3.4", t0 + 1000)
        inc.event("deploy:w", "🚀 Deploy: w restarted with a new version", t0 + 1100)
        text = incidents.weekly_text(cfg, inc, None, t0 + 2000)
        self.assertIn("Incidents: 1", text)
        self.assertIn("IP autoban — 1", text)
        self.assertIn("🚀 Planned: deploys — 1", text)
        self.assertIsNone(CYR.search(text), text)
        self.assertEqual(security.ban_duration_text(24), "for 24 h")
        self.assertEqual(security.ban_duration_text(168, bare=True), "7 d")
        self.assertEqual(checks.fmt_bytes(2048), "2.0 KB")
        self.assertEqual(checks.fmt_duration(3 * 3600 + 120), "3 h 2 min")

    def test_ukrainian(self):
        import checks
        i18n.set_lang("uk")
        self.assertEqual(checks.fmt_duration(90061), "1 д 1 год 1 хв")
        self.assertEqual(i18n._("❌ Контейнер {c} не работает ({st})", c="web", st="exited"),
                         "❌ Контейнер web не працює (exited)")


class LanguageButtonsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import test_bot  # общий config.yaml и data_dir во временной папке
        sys.argv = ["bot.py", str(test_bot.CONF)]
        import bot
        cls.bot = bot

    def setUp(self):
        self.bot.S.reset()

    def tearDown(self):
        self.bot.S.reset()
        i18n.set_lang("ru")

    def update(self, text=None, data=None):
        upd = mock.MagicMock()
        upd.effective_chat.id = 42
        upd.effective_chat.type = "private"
        upd.effective_message.text = text
        upd.effective_message.reply_text = mock.AsyncMock()
        q = upd.callback_query
        q.data = data
        q.answer = mock.AsyncMock()
        q.edit_message_text = mock.AsyncMock()
        ctx = mock.MagicMock()
        ctx.bot.set_my_commands = mock.AsyncMock()
        ctx.args = []
        return upd, ctx

    def test_switch_from_settings(self):
        b = self.bot
        self.assertIn("set:lang", [x.callback_data for row in b.settings_kb().inline_keyboard for x in row])
        upd, ctx = self.update(data="set:lang")
        asyncio.run(b.on_button(upd, ctx))
        markup = upd.callback_query.edit_message_text.await_args.kwargs["reply_markup"]
        self.assertEqual([x.callback_data for row in markup.inline_keyboard for x in row],
                         ["lang:ru", "lang:uk", "lang:en", "set"])

        upd, ctx = self.update(data="lang:en")
        asyncio.run(b.on_button(upd, ctx))
        self.assertEqual(i18n.lang(), "en")
        self.assertEqual(b.CFG["lang"], "en")
        self.assertIn('"lang": "en"', b.S.path.read_text(encoding="utf-8"))   # сохранено в settings.json
        upd.callback_query.answer.assert_awaited_with("Saved")
        sent = upd.effective_message.reply_text.await_args
        self.assertEqual(sent.args[0], "✅ Interface language: 🇬🇧 English")
        kb = sent.kwargs["reply_markup"]                                          # новая клавиатура внизу
        self.assertEqual(kb.keyboard[0][0].text, "📊 Status")
        cmds = ctx.bot.set_my_commands.await_args.args[0]                         # меню команд тоже
        self.assertEqual(cmds[0].description, "📊 Server status")
        settings = upd.callback_query.edit_message_text.await_args.args[0]
        self.assertIn("🛠 <b>Settings</b>", settings)
        self.assertIn("Language: 🇬🇧 English", settings)

    def test_text_in_all_languages(self):
        b = self.bot
        i18n.set_lang("en")
        b.HIST.peaks = mock.MagicMock(return_value=None)
        views = [b.settings_text(), b.alerts_text(), b.thresholds_view()[0], b.security_view()[0],
                 b.smart_view()[0], b.summary_view()[0], b.docker_view()[0], b.probes_view()[0]]
        for markup in (b.settings_kb(), b.alerts_kb(), b.alert_kb("ctr:web", True), b.status_kb(),
                       b.security_view()[1], b.backup_kb()):
            views += [x.text for row in markup.inline_keyboard for x in row if x.text != b.LANG_BTN]
        for v in views:
            self.assertIsNone(CYR.search(v), v)

    def test_lang_command_and_old_keyboard(self):
        b = self.bot
        upd, ctx = self.update()
        ctx.args = ["ua"]
        asyncio.run(b.cmd_lang(upd, ctx))
        self.assertEqual(i18n.lang(), "uk")
        self.assertEqual(upd.effective_message.reply_text.await_args.args[0], "✅ Мова інтерфейсу: 🇺🇦 Українська")
        # кнопка от старой русской клавиатуры всё ещё работает, ответ уже по-украински
        upd, ctx = self.update(text="📊 Статус")
        with mock.patch.object(b.checks, "status_text", return_value="ok"), \
                mock.patch.object(b, "cmd_status", wraps=b.cmd_status) as st:
            b.TEXT_BUTTONS[b.B_STATUS] = st
            try:
                asyncio.run(b.on_text(upd, ctx))
            finally:
                b.TEXT_BUTTONS[b.B_STATUS] = b.cmd_status
        st.assert_awaited()
        self.assertEqual(upd.effective_message.reply_text.await_args.kwargs["reply_markup"]
                         .inline_keyboard[0][0].text, "🔄 Оновити")
        # без аргумента — меню выбора
        upd, ctx = self.update()
        asyncio.run(b.cmd_lang(upd, ctx))
        self.assertIn("Мова інтерфейсу", upd.effective_message.reply_text.await_args.args[0])

    def test_reset_returns_to_config_language(self):
        b = self.bot
        b.S.set("lang", "en")
        self.assertEqual(i18n.lang(), "en")
        b.S.reset()
        self.assertEqual(i18n.lang(), "ru")


if __name__ == "__main__":
    unittest.main()
