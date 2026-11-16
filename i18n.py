"""Язык интерфейса: русский (исходные строки в коде), украинский и английский.

Строка в коде пишется по-русски и оборачивается в _(): _("Диск {name} заполнен", name=n).
Перевод ищется в locales.py по русскому тексту; если его нет, выводится русский.
N_() только помечает строку для перевода (для констант уровня модуля), переводит её потом _(константа).
Язык один на весь бот (настройка lang в settings.json), меняется в «Настройки → Язык» или /lang.
"""
from __future__ import annotations

LANGS = {"ru": "🇷🇺 Русский", "uk": "🇺🇦 Українська", "en": "🇬🇧 English"}
DEFAULT = "ru"

_lang = DEFAULT


def set_lang(code: str | None) -> str:
    global _lang
    _lang = code if code in LANGS else DEFAULT
    return _lang


def lang() -> str:
    return _lang


def _table(code: str) -> dict:
    import locales
    return {"uk": locales.UK, "en": locales.EN}.get(code, {})


def tr(text: str, code: str | None = None, **kw) -> str:
    """Перевод на язык code (по умолчанию текущий). С аргументами подставляет их через str.format."""
    code = code or _lang
    out = text if code == "ru" else _table(code).get(text, text)
    if kw:
        try:
            return out.format(**kw)
        except (KeyError, IndexError, ValueError):  # испорченный перевод не должен ронять алерт
            return text.format(**kw)
    return out


def _(text: str, **kw) -> str:
    return tr(text, None, **kw)


def N_(text: str) -> str:
    """Пометить строку для перевода, не переводя сейчас (константы, которые создаются при импорте)."""
    return text


def variants(text: str) -> set[str]:
    """Строка на всех языках: например, чтобы узнать нажатую кнопку клавиатуры при любом языке."""
    return {tr(text, code) for code in LANGS}
