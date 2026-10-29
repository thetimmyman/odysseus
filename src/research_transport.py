"""Trusted caller-owned I/O for isolated research replays.

The normal application keeps its existing model/search/fetch paths. A local
replay may supply this dependency to use an admitted frozen corpus and a model
transport such as a subscription CLI. Routes never deserialize a transport
from user input. Source permissions and transport credentials stay with its
caller; a response supplies observations, never authorization or acceptance.
"""
from dataclasses import dataclass
from typing import Dict, List, Optional, Protocol


@dataclass(frozen=True)
class ResearchModelReply:
    text: str
    usage: Optional[Dict[str, int]] = None

    def __post_init__(self):
        if not isinstance(self.text, str):
            raise TypeError("research model text must be a string")
        if self.usage is not None:
            if not isinstance(self.usage, dict) or any(
                not isinstance(self.usage.get(key), int)
                or isinstance(self.usage[key], bool) or self.usage[key] < 0
                for key in ("input_tokens", "output_tokens")
            ):
                raise ValueError("observed research usage requires nonnegative input/output token counts")
            object.__setattr__(self, "usage", dict(self.usage))


class ResearchTransport(Protocol):
    async def generate(self, *, model: str, messages: List[Dict], temperature: float,
                       max_tokens: int, timeout: int) -> ResearchModelReply: ...

    async def search(self, query: str, count: int) -> List[Dict]: ...

    async def fetch(self, url: str, timeout: int) -> Dict: ...
