"""Pure shared identifier validation; no optional runtime imports."""

class RunnerIntegrityError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def require(condition, code):
    if not condition:
        raise RunnerIntegrityError(code)


def name(value):
    require(type(value) is str and bool(value) and len(value.encode("utf-8")) <= 256, "runner_name")

