"""A stable text form for dictionary keys, shared by the tracer and the planner."""


def key_repr(k):
    if isinstance(k, str):
        return "s:" + k
    if isinstance(k, type):
        return "n:" + k.__name__
    name = getattr(k, "name", None)
    if isinstance(name, str) and type(k).__module__.startswith("sqlglot"):
        return "n:" + name  # enum members such as TokenType.COMMA
    if isinstance(k, tuple):
        return "t:" + ",".join(key_repr(x) for x in k)
    return "r:" + repr(k)[:80]
