class AppError(Exception):
    def __init__(self, status_code: int, code: str, message: str, headers: dict | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.headers = headers


def not_found(name: str = "Object") -> AppError:
    return AppError(404, "not_found", f"{name} not found")


def conflict(message: str) -> AppError:
    return AppError(409, "conflict", message)
