"""The user's chosen assistant name and conversational style.

Chosen once during onboarding (``kova setup``, the desktop's first connection,
or the chat's first run), saved in ``config.toml`` under ``[persona]``, and
used everywhere the assistant's identity appears: the system prompt, the
chat's reply header and welcome card, and the desktop's window and tray.
"""
from dataclasses import dataclass

# tone -> the one line it adds to the identity prompt.
TONES = {
    'warm': 'Be warm, direct and genuinely helpful.',
    'concise': 'Be concise, direct and genuinely helpful.',
    'professional': 'Be professional, clear and genuinely helpful.',
    'playful': 'Be playful, friendly and genuinely helpful.',
}

DEFAULT_NAME = 'Kova'


@dataclass(frozen=True)
class Persona:
    name: str = DEFAULT_NAME
    tone: str = 'warm'

    def __post_init__(self):
        name = self.name.strip()
        if not name or len(name) > 40 or any(ord(c) < 32 for c in name):
            raise ValueError('Name must be 1-40 printable characters.')
        if self.tone not in TONES:
            raise ValueError(f'Unknown tone {self.tone!r}. Choose one of: {", ".join(TONES)}.')
        object.__setattr__(self, 'name', name)

    @property
    def instruction(self) -> str:
        return TONES[self.tone]


__all__ = ['DEFAULT_NAME', 'TONES', 'Persona']
