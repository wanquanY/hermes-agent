"""Canonical MEDIA directive parsing contract."""

from channels.platforms.media_tags import MEDIA_TAG_CLEANUP_RE


def _paths(text: str) -> list[str]:
    return [match.group("path") for match in MEDIA_TAG_CLEANUP_RE.finditer(text)]


def test_adjacent_directives_are_distinct():
    assert _paths(
        "`MEDIA:/tmp/first.png` and MEDIA:\"/tmp/second.jpg\""
    ) == ["/tmp/first.png", '"/tmp/second.jpg"']


def test_unquoted_absolute_path_can_contain_spaces():
    assert _paths("MEDIA:/tmp/a useful screenshot.png") == [
        "/tmp/a useful screenshot.png"
    ]


def test_relative_unquoted_path_is_not_a_directive():
    assert _paths("MEDIA:../../etc/passwd.png") == []


def test_windows_absolute_path_is_recognized():
    assert _paths(r"MEDIA:C:\\Users\\agent\\shot.PNG") == [
        r"C:\\Users\\agent\\shot.PNG"
    ]
