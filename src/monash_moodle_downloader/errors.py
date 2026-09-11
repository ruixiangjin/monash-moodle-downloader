"""User-facing application errors."""


class MmdError(Exception):
    """Base class for expected downloader failures."""


class BrowserUnavailableError(MmdError):
    """Raised when Google Chrome cannot be opened."""


class LoginRequiredError(MmdError):
    """Raised when the saved Monash session is missing or expired."""


class MoodleApiError(MmdError):
    """Raised when Moodle rejects or cannot complete an AJAX request."""


class CourseNotFoundError(MmdError):
    """Raised when a course selector does not match a visible course."""
