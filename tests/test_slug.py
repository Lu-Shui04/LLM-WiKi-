"""slug 规范化：规则见 SCHEMA.md 第五节。"""
import unicodedata

from app.wiki.slug import slugify, unique_slug


def test_中文原样保留():
    assert slugify("意图路由") == "意图路由"


def test_剔除_windows_非法字符():
    assert slugify('a/b\\c:d*e?f"g<h>i|j') == "abcdefghij"


def test_空格转连字符():
    assert slugify("RAG 切块 策略") == "RAG-切块-策略"


def test_全角空格也转():
    assert slugify("RAG　切块") == "RAG-切块"


def test_连续空白折叠成一个连字符():
    assert slugify("a   b") == "a-b"


def test_去首尾连字符和点():
    assert slugify(" -a- ") == "a"
    assert slugify("...a...") == "a"


def test_截断六十字符():
    assert len(slugify("啊" * 100)) == 60


def test_截断后不留尾巴连字符():
    s = slugify("a" * 59 + " " + "b" * 10)
    assert len(s) <= 60
    assert not s.endswith("-")


def test_全是非法字符时兜底():
    s = slugify("///")
    assert s.startswith("page-")
    assert len(s) == len("page-") + 8


def test_空标题兜底():
    assert slugify("").startswith("page-")
    assert slugify(None).startswith("page-")


def test_nfc_规范化():
    decomposed = unicodedata.normalize("NFD", "café")
    assert slugify(decomposed) == unicodedata.normalize("NFC", "café")


def test_unique_不撞时原样返回():
    assert unique_slug("RAG", ["别的"]) == "RAG"


def test_unique_大小写不敏感():
    # NTFS 不区分大小写，这两个是同一个文件
    assert unique_slug("RAG", ["rag"]) == "RAG-2"


def test_unique_连撞多个():
    assert unique_slug("a", ["a", "A-2"]) == "a-3"
