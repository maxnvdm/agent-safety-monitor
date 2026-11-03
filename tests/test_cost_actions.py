import pytest

from monitor.cost.actions import bash_action, classify, family_of


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("git status -sb && git log --oneline", "git status"),
        ("cd /repo && uv run pytest -q", "uv run pytest"),
        ("cd /repo; FOO=1 uv run --frozen ruff check .", "uv run ruff"),
        ("python3 -m pytest tests/", "python -m pytest"),
        ("python script.py", "python"),
        ("curl -sS https://x | jq .", "curl"),
        ("sudo apt-get install -y foo", "apt-get install"),
        ("docker build -t x .", "docker build"),
        ("/usr/bin/ls -la", "ls"),
        ("echo hi", "(shell builtin)"),
        ("pnpm --dir fe lint", "pnpm"),
        ("", "(shell builtin)"),
    ],
)
def test_bash_action(command: str, expected: str) -> None:
    assert bash_action(command) == expected


def test_families() -> None:
    assert family_of("Read") == "file"
    assert family_of("Grep") == "search"
    assert family_of("Task") == "agent"
    assert family_of("mcp__github__get_file_contents") == "mcp:github"
    assert family_of("SomethingNew") == "other"


def test_classify_returns_family_and_action() -> None:
    assert classify("Bash", "cd x && git diff") == ("shell", "git diff")
    assert classify("mcp__github__pull_request_read", "{}") == ("mcp:github", "pull_request_read")
    assert classify("Read", "a.py") == ("file", "Read")
