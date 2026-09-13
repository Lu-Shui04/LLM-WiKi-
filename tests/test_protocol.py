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


def test_总览页大小写不敏感():
    # _FILE 本身带 IGNORECASE，模型写 Overview.md 不该让整次编译作废
    assert check_rel("Overview.md") == "overview.md"
    assert check_rel("OVERVIEW.MD") == "overview.md"


# ─── Windows 文件名硬伤：写盘会「成功」但磁盘上没有文件 ──────────
def test_中文冒号被拒():
    # NTFS 把 : 当数据流（ADS）：会在磁盘上留下一个叫 chunkSize 的空文件，
    # 正文进 ADS，此后 read_all 永远读不到这一页——静默丢页。
    with pytest.raises(ProtocolError):
        check_rel("concepts/chunkSize: 500.md")


def test_其他非法字符被拒():
    for bad in ('concepts/丙?.md', 'concepts/甲|乙.md', 'concepts/甲*.md',
                'concepts/甲"乙.md', 'concepts/甲<乙.md', 'concepts/甲>乙.md'):
        with pytest.raises(ProtocolError):
            check_rel(bad)


def test_保留设备名被拒():
    # 即使带扩展名，写它 write_text 也会成功返回，而磁盘上什么都没有
    with pytest.raises(ProtocolError):
        check_rel("concepts/nul.md")
    with pytest.raises(ProtocolError):
        check_rel("concepts/AUX.md")
    with pytest.raises(ProtocolError):
        check_rel("concepts/COM1.md")


def test_以点结尾被拒():
    # Windows 会静默吃掉文件名结尾的点和空格，写出来的文件名和 rel 对不上，
    # 于是 by_rel 查不到它、页面掉进「计划外页面」分支。
    # （结尾空格那一种到不了这里——check_rel 开头就把整个 rel strip 掉了。）
    with pytest.raises(ProtocolError):
        check_rel("concepts/甲.md.")
    with pytest.raises(ProtocolError):
        check_rel("concepts/甲.md..")


def test_名字里带点的正常文件不受影响():
    # 只有**结尾**的点是硬伤，concepts/v1.2 这种中间带点的必须放行
    assert check_rel("concepts/RAG 2.0.md") == "concepts/RAG 2.0.md"
