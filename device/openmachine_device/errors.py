"""Stable diagnostics never include keys or file contents."""


class ConnectorError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
