"""Public errors. Messages never contain credentials or server response bodies."""


class SkivaError(Exception):
    """Base SDK error."""


class ProtocolError(SkivaError):
    pass


class IntegrityError(ProtocolError):
    pass


class LimitExceeded(SkivaError):
    pass


class ClosedError(SkivaError):
    pass


class AuthorizationDenied(SkivaError):
    def __init__(self, code: str = "FORBIDDEN"):
        self.code = code
        super().__init__("Skiva denied access")


class TransportError(SkivaError):
    def __init__(self, status_code: int | None = None):
        self.status_code = status_code
        super().__init__("Skiva distribution request failed")


class VersionMismatch(SkivaError):
    def __init__(self, expected: str, actual: str):
        self.expected = expected
        self.actual = actual
        super().__init__("Skill version changed; refresh discovery and load again")


class ManifestChanged(SkivaError):
    """The authorized file set for a captured snapshot changed."""


class SkillNotFound(SkivaError):
    pass
