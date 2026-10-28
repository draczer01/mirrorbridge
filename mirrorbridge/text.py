"""Words for a person, in English, that also say which gui.i18n string they are.

The command line and the log take a Text as the plain English str it is. The GUI shows it
in the user's language: gui.i18n.say() looks its key up and fills in the same values,
each a str or a Text of its own. An error carries one as its message, and
gui.i18n.error_text() finds it there, as gui.extend finds a VddError's code.
"""

from __future__ import annotations


class Text(str):
    """`english` (a str.format template) filled in with `values`; gui.i18n has `key` in
    English and its other languages, with the same {placeholders}."""

    key: str
    values: dict

    def __new__(cls, key: str, english: str, **values: object) -> "Text":
        text = super().__new__(cls, english.format(**values))
        text.key = key
        text.values = values
        return text
