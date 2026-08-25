class LLVideoError(Exception):
    """Base error. Message is shown to the user verbatim."""

class MissingDependency(LLVideoError):
    pass

class ProbeFailed(LLVideoError):
    pass

class NoProvider(LLVideoError):
    pass

class ProviderError(LLVideoError):
    pass

class TooLong(LLVideoError):
    pass
