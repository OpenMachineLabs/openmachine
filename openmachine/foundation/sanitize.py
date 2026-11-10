"""Bounded, safe public errors."""


class Fault(Exception):
    def __init__(self, code: str, message: str, *, effect: str | None = None):
        super().__init__(message)
        if effect not in {None, "known_no_effect", "effect_unknown"}:
            raise ValueError("invalid owner effect classification")
        self.code = code
        self.effect = effect
