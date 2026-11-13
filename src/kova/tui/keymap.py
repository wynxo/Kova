"""TUI action bindings, represented by the key names emitted by KeyReader.

``DEFAULT_KEYS`` maps each action the chat responds to, to the ``KeyReader``
key name that triggers it. ``config.toml`` may override any of them under
``[tui.keys]`` (see the README); ``Keymap.translate`` then turns whatever the
terminal sends back into the canonical key name for its action, so the rest
of the chat never sees the remapping.
"""
import re

from .terminal import Key

DEFAULT_KEYS = {
    'shortcuts': 'question', 'palette': 'ctrl-p', 'thinking': 'ctrl-t', 'tools': 'ctrl-o',
    'scroll_up': 'pageup', 'scroll_down': 'pagedown', 'cancel': 'esc',
    'interrupt': 'ctrl-c', 'exit': 'ctrl-d', 'permissions': 'shift-tab',
    'clear': 'clear-screen', 'suspend': 'suspend', 'up': 'up', 'down': 'down',
    'complete': 'tab', 'submit': 'enter', 'newline': 'newline',
    'backspace': 'backspace', 'delete': 'delete', 'left': 'left', 'right': 'right',
    'home': 'home', 'end': 'end', 'word_left': 'word-left', 'word_right': 'word-right',
    'kill_end': 'kill-end', 'kill_start': 'kill-start', 'kill_word': 'kill-word',
    'kill_word_right': 'kill-word-right', 'yank': 'yank', 'next': 'ctrl-n',
}

# How the default key names read on screen; anything else shows as its name.
_DISPLAY = {'clear-screen': 'ctrl+l', 'question': '?', 'suspend': 'ctrl+z',
            'enter': '⏎', 'newline': 'shift+⏎', 'pageup': 'pgup', 'pagedown': 'pgdn'}
_HELP_LABELS = {'esc': 'cancel', 'ctrl+p': 'palette', 'ctrl+c': 'interrupt',
                'ctrl+t': 'thinking', 'ctrl+o': 'tools', 'ctrl+d': 'exit',
                'shift+tab': 'permissions', 'ctrl+l': 'clear', 'tab': 'complete',
                'pgup': 'scroll_up', 'pgdn': 'scroll_down', 'ctrl+j': 'newline'}
_HELP_PATTERN = re.compile(r'ctrl\+[a-z]+|shift\+tab|pgup|pgdn|(?<![\w+])(?:esc|tab)(?![\w])')


class Keymap:
    def __init__(self, overrides=None):
        overrides = overrides or {}
        unknown = set(overrides) - DEFAULT_KEYS.keys()
        if unknown:
            raise ValueError('Unknown TUI actions: ' + ', '.join(sorted(unknown)))
        self.bindings = {**DEFAULT_KEYS, **overrides}
        if any(not isinstance(key, str) or not key for key in self.bindings.values()):
            raise ValueError('Keys must be non-empty key names')
        if len(set(self.bindings.values())) != len(self.bindings):
            raise ValueError('TUI key bindings must be unique; swap both actions')
        available = set(DEFAULT_KEYS.values()) | {f'ctrl-{c}' for c in 'grvx'}
        if set(self.bindings.values()) - available:
            raise ValueError('Unsupported terminal key name')
        self._reverse = {physical: DEFAULT_KEYS[action] for action, physical in self.bindings.items()}

    def translate(self, key):
        if key.name in self._reverse:
            return Key(self._reverse[key.name], key.text)
        if key.name in DEFAULT_KEYS.values():
            return Key('unbound')
        return key

    def format_help(self, text):
        """Swap each key written the default way in ``text`` for its binding."""
        return _HELP_PATTERN.sub(lambda m: self.pretty(_HELP_LABELS[m.group()]), text)

    def label(self, action):
        return self.bindings[action]

    def pretty(self, action):
        """The binding as it reads on screen (``ctrl+p``, ``?``, ``pgup``)."""
        shown = _DISPLAY.get(self.bindings[action], self.bindings[action])
        return shown.replace('ctrl-', 'ctrl+').replace('shift-', 'shift+')
