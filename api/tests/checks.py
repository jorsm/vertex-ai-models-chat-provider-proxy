"""Assertion helpers for the test suite.

Each helper raises `AssertionError` with an explanatory message, so pytest reports
failures normally without requiring bare `assert` statements.
"""

from typing import Any


def _fail(message: str, default: str) -> None:
    """Raise `AssertionError` with the caller's message or the default description."""
    raise AssertionError(message or default)


def expect(condition: object, message: str = "") -> None:
    """Require a truthy condition."""
    if not condition:
        _fail(message, "Expected a truthy value")


def expect_equal(actual: Any, expected: Any, message: str = "") -> None:
    """Require `actual == expected`."""
    if actual != expected:
        _fail(message, f"{actual!r} != {expected!r}")


def expect_not_equal(actual: Any, unexpected: Any, message: str = "") -> None:
    """Require `actual != unexpected`."""
    if actual == unexpected:
        _fail(message, f"{actual!r} == {unexpected!r}")


def expect_in(member: Any, container: Any, message: str = "") -> None:
    """Require `member in container`."""
    if member not in container:
        _fail(message, f"{member!r} not found in {container!r}")


def expect_not_in(member: Any, container: Any, message: str = "") -> None:
    """Require `member not in container`."""
    if member in container:
        _fail(message, f"{member!r} unexpectedly found in {container!r}")


def expect_is(actual: Any, expected: Any, message: str = "") -> None:
    """Require `actual is expected`."""
    if actual is not expected:
        _fail(message, f"{actual!r} is not {expected!r}")


def expect_is_not(actual: Any, unexpected: Any, message: str = "") -> None:
    """Require `actual is not unexpected`."""
    if actual is unexpected:
        _fail(message, f"{actual!r} is {unexpected!r}")


def expect_greater(actual: Any, bound: Any, message: str = "") -> None:
    """Require `actual > bound`."""
    if not actual > bound:
        _fail(message, f"{actual!r} is not greater than {bound!r}")


def expect_greater_equal(actual: Any, bound: Any, message: str = "") -> None:
    """Require `actual >= bound`."""
    if not actual >= bound:
        _fail(message, f"{actual!r} is not greater than or equal to {bound!r}")


def expect_less(actual: Any, bound: Any, message: str = "") -> None:
    """Require `actual < bound`."""
    if not actual < bound:
        _fail(message, f"{actual!r} is not less than {bound!r}")


def expect_less_equal(actual: Any, bound: Any, message: str = "") -> None:
    """Require `actual <= bound`."""
    if not actual <= bound:
        _fail(message, f"{actual!r} is not less than or equal to {bound!r}")
