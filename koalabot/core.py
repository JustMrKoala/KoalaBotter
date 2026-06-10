"""Core shared logic ported from the original quizBots (names, clamping, models)."""

from __future__ import annotations
import re
from dataclasses import dataclass, field, asdict
from typing import List, Optional, Dict, Any

MAX_BOTS = 200  # matches the original implementation

def clamp_number(value: int | str, min_v: int, max_v: int, default: int) -> int:
    try:
        v = int(value)
    except (TypeError, ValueError):
        v = default
    return max(min_v, min(max_v, v))

def build_player_names(prefix: str, count: int) -> List[str]:
    """Exact match to original shared.js:
    const base = prefix?.trim() || 'Bot';
    return Array.from({ length: count }, (_, i) => `${base}${count > 1 ? ` ${i + 1}` : ''}`);
    Examples: count=1 → ["Bot"], count=5 → ["Bot 1", "Bot 2", "Bot 3", "Bot 4", "Bot 5"]
    """
    base = (prefix or "Bot").strip() or "Bot"
    if count <= 1:
        return [base]
    return [f"{base} {i + 1}" for i in range(count)]

@dataclass
class PlayerStatus:
    name: str
    platform: str = ""
    joined: bool = False
    status: str = "pending"  # pending, joining, joined, answering, completed, failed, disconnected, join_timeout, etc.
    answers: int = 0
    error: Optional[str] = None
    end_reason: Optional[str] = None
    last_action: str = ""

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        # keep backward compat with old field names in summary consumers
        d.setdefault("error", self.error)
        return d

@dataclass
class BotRunResult:
    platform: str
    game_pin: str
    join_url: str
    config: Dict[str, Any]
    summary: Dict[str, Any]
    players: List[Dict[str, Any]]
    note: str

def make_summary(players: List[PlayerStatus]) -> Dict[str, Any]:
    attempted = len(players)
    joined = sum(1 for p in players if p.joined)
    failed = attempted - joined
    completed = sum(1 for p in players if p.status == "completed")
    total_answers = sum(p.answers for p in players)
    success_rate = f"{round((joined / attempted) * 100)}%" if attempted else "0%"
    return {
        "attempted": attempted,
        "joined": joined,
        "failed": failed,
        "completed": completed,
        "totalAnswers": total_answers,
        "successRate": success_rate,
    }

def normalize_platform(value: str) -> str:
    key = (value or "").strip().lower()
    if key in ("kahoot", "k"):
        return "kahoot"
    if key in ("blooket", "b"):
        return "blooket"
    if key in ("gimkit", "g"):
        return "gimkit"
    if key in ("lessonup", "lesson", "lu", "lesson-up"):
        return "lessonup"
    raise ValueError("Platform must be Kahoot, Blooket, Gimkit, or LessonUp")

PLATFORMS_META = {
    "kahoot": {
        "label": "Kahoot",
        "join_url": lambda pin: f"https://kahoot.it/?pin={pin}",
    },
    "blooket": {
        "label": "Blooket",
        "join_url": lambda pin: f"https://www.blooket.com/play/{pin}",
    },
    "gimkit": {
        "label": "Gimkit",
        "join_url": lambda pin: f"https://www.gimkit.com/join/{pin}",
    },
    "lessonup": {
        "label": "LessonUp",
        "join_url": lambda pin: f"https://lessonup.app/?code={pin}",
    },
}
