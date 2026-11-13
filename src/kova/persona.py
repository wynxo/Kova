"""The user's chosen assistant name and conversational style."""
from dataclasses import dataclass

TONES = {
    'warm': 'Be warm, direct and genuinely helpful.',
    'concise': 'Be concise, direct and genuinely helpful.',
    'professional': 'Be professional, clear and genuinely helpful.',
    'playful': 'Be playful, friendly and genuinely helpful.',
}


@dataclass(frozen=True)
class Persona:
    name: str = 'Kova'
    tone: str = 'warm'

    def __post_init__(self):
        if not self.name.strip() or len(self.name) > 40 or any(ord(c) < 32 for c in self.name):
            raise ValueError('Name must be 1–40 printable characters.')
        if self.tone not in TONES:
            raise ValueError('Unknown tone.')

    @property
    def instruction(self) -> str:
        return TONES[self.tone]
