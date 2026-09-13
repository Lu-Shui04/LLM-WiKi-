"""集中配置：全项目只有这里读环境变量 / .env，其他模块一律 from app.config import settings

优先级：真实环境变量 > .env 文件 > 代码里的默认值
自检：python -m app.config
"""
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# 用绝对路径定位，避免 `python -m app.scripts.xxx` 时 .env 没被加载、key 静默变 None
_ENV_FILE = Path(__file__).resolve().parent.parent / ".env"


def _mask(value: str) -> str:
    """打码展示，防止 key 整串漏进终端或日志"""
    if not value:
        return "（未设置）"
    return "***" if len(value) <= 10 else f"{value[:6]}...{value[-4:]}"


class ConfigError(RuntimeError):
    """缺必需配置。

    单独一个类型是为了让调用方能**只**接住这一类，而不是把所有 RuntimeError
    都当成配置问题——那样别的真 bug 会被误导成「去填 .env」。
    继承 RuntimeError 是刻意的：不破坏既有的 except RuntimeError。
    """


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=_ENV_FILE,
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ─── DeepSeek 对话模型 ───────────────────────────────
    # 官方模型名只有 deepseek-flash / deepseek-v4-pro
    deepseek_api_key: str = ""
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_model: str = "deepseek-flash"
    # Wiki 编译专用。编译是一次性深度加工，产物质量决定之后所有问答的质量，
    # 所以用更强的模型；日常问答仍走上面的 flash。
    ingest_model: str = "deepseek-v4-pro"

    # ─── 智谱 AI（Embedding）─────────────────────────────
    zhipu_api_key: str = ""
    zhipu_base_url: str = "https://open.bigmodel.cn/api/paas/v4"
    embedding_model: str = "embedding-3"
    embedding_dim: int = 1024  # 维度建表时固化，改了必须重建索引

    # ─── 知识库（原始素材层，只读）───────────────────────
    knowledge_dir: Path = Path(__file__).resolve().parent.parent / "knowledge"

    # ─── LLM Wiki（编译产物层）──────────────────────────
    # wiki/ 是纯派生物：任何时刻都能从 knowledge/ + SCHEMA.md 重编出来，
    # 所以它坏了不用修，删掉重跑即可。
    wiki_dir: Path = Path(__file__).resolve().parent.parent / "wiki"
    schema_file: Path = Path(__file__).resolve().parent.parent / "SCHEMA.md"
    # 页面数不超过这个值时，query 把全部页面全文注入上下文。
    # 超过才切成「代码打分预筛」，因为那时全量注入的 token 成本开始不划算。
    wiki_inline_max_pages: int = 15
    # "wiki" 走 LLM Wiki；"legacy" 回退旧的向量检索。
    # 注意：本阶段 chat.py 还没接这个开关，它只是先声明出来。
    chat_backend: str = "wiki"

    # ─── 提示词 ─────────────────────────────────────────
    # 必须和 knowledge_dir 分开：knowledge/ 会被 ingest 切块嵌进向量库，提示词不能进去
    prompts_dir: Path = Path(__file__).resolve().parent.parent / "prompts"

    # ─── PostgreSQL + pgvector ──────────────────────────
    pg_host: str = "localhost"
    pg_port: int = 5432
    pg_user: str = "postgres"
    pg_password: str = "postgres"
    pg_database: str = "personal_agent"

    @property
    def pg_dsn(self) -> str:
        """psycopg3 连接串，给 langchain_postgres / SQLAlchemy 用"""
        return (
            f"postgresql+psycopg://{self.pg_user}:{self.pg_password}"
            f"@{self.pg_host}:{self.pg_port}/{self.pg_database}"
        )

    @property
    def pg_dsn_safe(self) -> str:
        """密码打码版本，可以安全打印"""
        return (
            f"postgresql+psycopg://{self.pg_user}:***"
            f"@{self.pg_host}:{self.pg_port}/{self.pg_database}"
        )

    def require(self, *fields: str) -> None:
        """检查必需配置。缺失时直接抛带指引的错误，好过 None 传到 API 才报 401"""
        missing = [f for f in fields if not getattr(self, f, "")]
        if missing:
            names = "\n".join(f"  - {f.upper()}" for f in missing)
            raise ConfigError(f"\n缺少必需配置：\n{names}\n请填写：{_ENV_FILE}\n")

    def report(self) -> None:
        print(f".env：{_ENV_FILE}  {'已找到' if _ENV_FILE.exists() else '不存在'}")
        print(f"DeepSeek  key={_mask(self.deepseek_api_key)}  model={self.deepseek_model}")
        print(f"智谱      key={_mask(self.zhipu_api_key)}  model={self.embedding_model} dim={self.embedding_dim}")
        print(f"PG        {self.pg_dsn_safe}")
        print(f"编译      model={self.ingest_model}")
        print(f"Wiki      {self.wiki_dir}")
        print(f"          内联上限 {self.wiki_inline_max_pages} 页  后端 {self.chat_backend}")


settings = Settings()


if __name__ == "__main__":
    settings.report()
