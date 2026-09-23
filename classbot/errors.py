class NotLoggedIn(Exception):
    """Бот не вошёл в аккаунт (Google или Zoom)."""


class CallError(Exception):
    """Зайти в звонок не получилось, и повторять бессмысленно."""


class NotAdmitted(Exception):
    """Не впустили за отведённое время."""
