"""Exceptions shared across the package.

Kept free of heavy imports: the command line imports this module on every start, and
commands such as ``narsat db wait`` must not load the imaging stack.
"""


class ConfigurationError(RuntimeError):
    """A setting the command needs is missing or invalid; the message says which."""
