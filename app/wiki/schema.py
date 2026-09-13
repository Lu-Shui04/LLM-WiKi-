"""页面结构校验。规则全部来自 SCHEMA.md，两边必须一起改。

落盘前跑一遍。errors 非空就不许写盘（store 的两阶段提交靠它）。
"""
from app.wiki import paths

TYPES = ("summary", "entity", "concept", "comparison", "query", "overview")
CONTRADICTIONS = ("none", "open", "resolved")

REQUIRED_META = ("title", "type", "summary")    # LLM 必须写的三样
WARN_BODY_CHARS = 2000                          # 单页超这个长度提醒一句


def validate(rel: str, meta: dict, body: str) -> tuple[list[str], list[str]]:
    """→ (errors, warnings)。errors 非空 = 不能落盘。"""
    errs: list[str] = []
    warns: list[str] = []

    for k in REQUIRED_META:
        if not str(meta.get(k, "")).strip():
            errs.append(f"{rel}：frontmatter 缺 {k}")

    if "\n" in str(meta.get("summary", "")):
        errs.append(f"{rel}：summary 必须是单行")

    t = str(meta.get("type", "")).strip()
    if t and t not in TYPES:
        errs.append(f"{rel}：type={t} 不在 {list(TYPES)}")
    elif t:
        want = ("overview" if rel == paths.OVERVIEW
                else paths.DIR_TYPE.get(rel.split("/")[0]))
        if want and t != want:
            errs.append(f"{rel}：type={t} 与所在目录不符（应为 {want}）")

    c = str(meta.get("contradictions", "none")).strip() or "none"
    if c not in CONTRADICTIONS:
        errs.append(f"{rel}：contradictions={c} 不在 {list(CONTRADICTIONS)}")

    text = (body or "").strip()
    if not text:
        errs.append(f"{rel}：正文为空")
    else:
        if "> [!WARNING] 矛盾" in text and c == "none":
            # 落盘前只提醒，不当场失败：模型偶有疏漏，整次编译作废代价太大。
            # lint 会把这种不一致报成 error，等人工确认。
            warns.append(f"{rel}：正文有矛盾块，但 contradictions 写着 none")
        if len(text) > WARN_BODY_CHARS:
            warns.append(
                f"{rel}：正文 {len(text)} 字，超过 {WARN_BODY_CHARS}（可能该拆页或该精简）"
            )

    return errs, warns
