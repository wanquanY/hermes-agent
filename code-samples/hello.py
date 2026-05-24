#!/usr/bin/env python3
"""Python sample: functions, classes, typing, and CLI entry."""
from dataclasses import dataclass
from typing import Iterable


@dataclass
class User:
    name: str
    score: int = 0


def average(values: Iterable[float]) -> float:
    values = list(values)
    return sum(values) / len(values) if values else 0.0


def main() -> None:
    users = [User("Ada", 98), User("Linus", 91), User("Grace", 100)]
    print("Python sample")
    print("Users:", ", ".join(f"{u.name}={u.score}" for u in users))
    print("Average:", average(u.score for u in users))


if __name__ == "__main__":
    main()
