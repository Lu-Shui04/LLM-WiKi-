"""===FILE:=== 多文件协议：见 SCHEMA.md 第十节。

解析要宽（模型书写习惯千奇百怪），路径校验要严（路径穿越会写出 wiki/ 之外）。
"""
import pytest

from app.wiki.protocol import ProtocolError, check_rel, parse_files


def test_正常多文件():
    text = (
        "===FILE: summaries/我的简历.md===\n"
        "---\ntitle: 我的简历\n---\n\n摘要正文\n"
        "===FILE: concepts/意图路由.md===\n"
        "正文二\n"
    )
    got = parse_files(text)
    assert [r for r, _ in got] == ["summaries/我的简历.md", "concepts/意图路由.md"]
    assert got[0][1].startswith("---")
    assert got[1][1] == "正文二"


def test_末段缺结尾等号也认():
    assert parse_files("===FILE: concepts/甲.md===\n只有正文") == [("concepts/甲.md", "只有正文")]


def test_被代码围栏包住也认():
    text = "```markdown\n===FILE: concepts/甲.md===\n正文\n```\n"
    assert parse_files(text) == [("concepts/甲.md", "正文")]


def test_大小写和空格不敏感():
    got = parse_files("=== file :  concepts/甲.md  ===\n正文")
    assert got == [("concepts/甲.md", "正文")]


def test_没有文件标记时报错():
    with pytest.raises(ProtocolError):
        parse_files("就是一段普通回答")


def test_overview_在根目录允许():
    assert check_rel("overview.md") == "overview.md"


def test_目录不在白名单被拒():
    with pytest.raises(ProtocolError):
        check_rel("other/甲.md")


def test_非_md_被拒():
    with pytest.raises(ProtocolError):
        check_rel("concepts/甲.txt")


def test_上级目录被拒():
    with pytest.raises(ProtocolError):
        check_rel("../甲.md")
    with pytest.raises(ProtocolError):
        check_rel("concepts/../../甲.md")


def test_绝对路径被拒():
    with pytest.raises(ProtocolError):
        check_rel("/etc/甲.md")
    with pytest.raises(ProtocolError):
        check_rel("C:/甲.md")


def test_多级目录被拒():
    with pytest.raises(ProtocolError):
        check_rel("concepts/sub/甲.md")


def test_反斜杠按分隔符处理():
    assert check_rel("concepts\\甲.md") == "concepts/甲.md"
