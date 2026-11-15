from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3


PROJECT_ROOT = Path(__file__).resolve().parents[1]
REAL_DB_PATH = PROJECT_ROOT / "data" / ("ayunos" + "_chatgpt" + ".db")
TEST_DB_PATH = PROJECT_ROOT / "data" / "test" / "ayunos_memory_test.db"
SOURCE_PLATFORMS = ("chatgpt", "deepseek", "manual")

LONG_SCENARIOS = [
    {
        "title": "骑行装备预算讨论",
        "tags": ["骑行", "减重", "装备"],
        "status": "project",
        "note": "synthetic cycling gear budget scenario",
        "messages": [
            "我想整理一套骑行装备预算，重点是锁鞋、骑行眼镜、减重和日常训练体验。",
            "可以先把目标拆成安全、效率和舒适三类，再决定哪些装备优先买。",
            "我现在纠结锁鞋是不是值得买，担心上锁不熟练，城市路口又多。",
            "锁鞋更适合稳定训练和长距离骑行，刚开始可以先在空地练习上锁和脱锁。",
            "骑行眼镜也想定制，主要是近视和防风，普通夹片好像不太舒服。",
            "定制骑行眼镜会提高稳定性，但预算要和锁鞋一起看，避免一次性投入太重。",
            "如果锁鞋加骑行眼镜预算约 3500 元，这样会不会太高？",
            "锁鞋 + 骑行眼镜预算约 3500 元属于中高档入门组合，建议先确认鞋码、锁片系统和镜片方案。",
            "我还想兼顾减重，车上还有一些小配件可以换。",
            "减重优先级可以放在轮组、外胎和随身装备精简，别为了轻牺牲安全。",
            "锁鞋如果买，锁踏也要一起算吧？",
            "是的，锁鞋、锁踏、锁片要成套考虑，还要预留调试时间。",
            "骑行眼镜要不要变色镜片？早晚骑车光线差别挺大。",
            "变色镜片适合全天候，但如果夜骑多，需要确认低光透光率。",
            "那 3500 里面，锁鞋锁踏大概 1800，眼镜 1200，剩下做锁片和配件？",
            "这个分配合理，可以把 500 元作为锁片、鞋垫、维护和试错预算。",
            "我担心买完装备训练频率不够，最后变成堆东西。",
            "可以设置 4 周骑行计划，完成 8 次训练后再下单第二件装备。",
            "那先买骑行眼镜还是先买锁鞋？",
            "如果当前最大痛点是风和视线，先买骑行眼镜；如果目标是踏频和效率，先买锁鞋。",
            "我觉得眼镜影响每次骑行，锁鞋更像进阶。",
            "那第一阶段买定制骑行眼镜，第二阶段在稳定训练后再买锁鞋。",
            "最后帮我把预算结论记一下：先眼镜，锁鞋待定，总预算控制在 3500 左右。",
            "结论：骑行眼镜优先，锁鞋作为训练稳定后的升级项，锁鞋和骑行眼镜总预算控制在约 3500 元。",
        ],
    },
    {
        "title": "AyunOS 第二大脑设计讨论",
        "tags": ["AyunOS", "第二大脑", "原文回溯"],
        "status": "project",
        "note": "synthetic AyunOS source trace design scenario",
        "messages": [
            "AyunOS 现在要从数据库后台变成第二大脑，核心不是字段，而是可读记忆。",
            "可以把目标定义成搜索、定位、上下文、完整原文回溯四步。",
            "测试库和真实库必须分离，默认只能看测试库。",
            "是的，真实库必须通过 real 命令显式打开，并在 UI 上显示写入警告。",
            "搜索结果如果只有 message_id 和 conversation_id，用户还是会觉得像数据库。",
            "所以搜索结果要做成命中卡片，显示标题、角色、时间、片段、标签和状态。",
            "Source Trace 的重点是不要总结，而是回到原文。",
            "可以设计 /api/message-context，根据 message_id 返回前后几条原文消息。",
            "然后完整对话页支持 focus_message_id，高亮命中消息。",
            "这样用户从摘要定位到原文回溯，路径就完整了。",
            "测试库需要足够长的 mock 对话，否则上下文只有一两句，看不出价值。",
            "长对话场景可以覆盖 AyunOS、骑行、学习、技术调试和写作项目。",
            "Project Hub 先不要做真实项目管理，只用关键词聚合测试库 mock 内容。",
            "对，项目视图是入口，不新增表，也不读取真实库。",
            "标签和状态可以写测试库 mark/tag 表，源 conversations/messages 不变。",
            "验证时要记录 conversations/messages 行数，证明原始表没有被修改。",
            "Web UI 中文化也要彻底，不能残留 Recent、Search、Add Tag 这些英文。",
            "角色 user 和 assistant 要显示成用户和助手，review 显示待整理。",
            "消息级操作也重要，每条消息旁边要能查看上下文和复制 message_id。",
            "定位此消息可以在完整对话里滚动到当前消息并高亮。",
            "健康检查里可以返回 source_trace_enabled 和 ui_language。",
            "还可以加 long_mock_scenarios_enabled，说明测试库支持长场景。",
            "文档要说明 Ctrl + F5 强制刷新，避免旧缓存让用户以为中文化没完成。",
            "最终边界仍然是不联网、不上传、不调用 API、不做 AI 总结、不接 Ollama、不做向量库。",
            "这就是 AyunOS P1.2 的目标：用测试库证明第二大脑原文回溯体验。",
            "实现时所有示例内容都必须是 synthetic mock，不复制真实标题、正文、ID 或时间线。",
        ],
    },
    {
        "title": "专升本复习计划",
        "tags": ["学习", "专升本", "阶段目标"],
        "status": "study",
        "note": "synthetic exam planning scenario",
        "messages": [
            "我想做一个专升本复习计划，数学和英语都要兼顾。",
            "先按阶段目标拆分：基础复盘、专项训练、套卷模拟和错题回收。",
            "数学基础不稳，函数和导数总是忘。",
            "数学第一阶段每天 90 分钟，先补函数、极限、导数和常用公式。",
            "英语主要是词汇和阅读，作文也怕写不出来。",
            "英语可以每天 40 个词汇、2 篇阅读、每周 2 篇作文模板训练。",
            "我每天能稳定学习 4 小时，周末可以 6 小时。",
            "工作日安排数学 90 分钟、英语 90 分钟、错题 40 分钟、复盘 20 分钟。",
            "阶段目标怎么定比较稳？",
            "第 1-4 周补基础，第 5-8 周做专项，第 9-12 周套卷，第 13 周后冲刺。",
            "如果中间落后怎么办？",
            "每周留半天机动时间，只补关键漏洞，不把计划排满。",
            "数学错题要怎么整理？",
            "按知识点、错误原因、正确解法和复盘日期记录，避免只抄题。",
            "英语阅读错题感觉很散。",
            "阅读错题按词汇、长难句、定位错误和选项陷阱分类。",
            "我还想每周看到进度，不然容易焦虑。",
            "每周日做一次学习复盘，记录完成率、薄弱点和下周阶段目标。",
            "阶段目标能不能写得具体一点？",
            "例如第 4 周前完成数学基础章节一轮，英语核心词汇完成 1200 个。",
            "模拟卷什么时候开始？",
            "第 9 周开始，每周 2 套，先不限时分析，再逐步限时。",
            "最后帮我总结一下这个专升本计划。",
            "总结：数学打基础，英语稳词汇阅读，按 4 周一个阶段推进，用错题和周复盘校正。",
        ],
    },
    {
        "title": "ComfyUI 调试记录",
        "tags": ["技术调试", "ComfyUI", "工作流"],
        "status": "tech",
        "note": "synthetic ComfyUI troubleshooting scenario",
        "messages": [
            "ComfyUI 工作流今天又报错，感觉像模型节点没有接好。",
            "先区分是模型路径、节点参数、显存不足还是工作流版本不兼容。",
            "报错出现在加载 checkpoint 之后，采样前就停了。",
            "那优先检查 checkpoint 节点输出是否连接到 sampler 的 model 输入。",
            "显存也可能不够，最近换了更大的分辨率。",
            "可以先把分辨率降到 768，再关掉高占用的预览节点做对比。",
            "如果节点缺失，是不是要装 custom nodes？",
            "是的，但先看报错里是否明确提示 node type missing，再决定是否安装。",
            "我不想乱装，怕环境更乱。",
            "可以复制一份工作流，保留原始文件，只在测试副本里修节点。",
            "模型路径应该放在哪里？",
            "checkpoint 放 models/checkpoints，VAE 放 models/vae，LoRA 放 models/loras。",
            "如果工作流引用旧模型名，会不会导致加载失败？",
            "会，节点里保存的是名称，改名后需要重新选择模型。",
            "显存报错一般怎么看？",
            "通常会出现 CUDA out of memory，可以降低 batch、尺寸或开启更省显存的选项。",
            "节点报错和模型报错经常混在一起。",
            "调试顺序建议是路径、缺失节点、参数范围、显存、再看插件版本。",
            "我想记录每次技术调试过程，方便下次搜索。",
            "可以给每条调试记录加标签，比如 ComfyUI、模型、节点、报错、显存、工作流。",
            "如果修好了，要不要保存一个稳定工作流？",
            "要，把稳定版本另存为 workflow_stable_mock.json，并记录关键模型和节点版本。",
            "这次先怎么处理？",
            "先降分辨率、检查 checkpoint 连接、确认缺失节点，再逐步恢复原工作流。",
        ],
    },
    {
        "title": "写作项目归档",
        "tags": ["写作", "小说", "草稿"],
        "status": "writing",
        "note": "synthetic writing archive scenario",
        "messages": [
            "我想把写作项目归档一下，小说、角色、世界观和草稿都散在不同地方。",
            "可以先按项目、角色、世界观、章节草稿、审阅意见五类整理。",
            "角色资料最乱，有些设定写在聊天里，有些在文档里。",
            "先给每个角色建立一张卡片，包含动机、关系、冲突和变化线。",
            "世界观设定也容易互相矛盾。",
            "世界观可以分成时间线、地点、规则、组织和禁忌，审阅时逐项查冲突。",
            "草稿版本很多，我不知道哪个是最新。",
            "建议每个章节保留当前稿、旧稿归档和审阅稿三类，不要混在一起。",
            "审阅意见要怎么沉淀？",
            "按结构问题、人物问题、节奏问题、语言问题分类，关联到章节。",
            "如果我要搜索某个角色，应该能看到相关草稿和设定。",
            "可以用标签：写作、小说、角色、世界观、草稿、审阅。",
            "我还想记录哪些段落需要重写。",
            "给需要重写的条目标记 review，备注写明重写原因。",
            "小说项目太大，先从哪里开始？",
            "先选一个主线角色和前三章草稿，做一次小范围归档。",
            "世界观是不是要先定死？",
            "不用定死，先记录当前版本和已知矛盾，后续审阅时再收敛。",
            "我担心归档变成拖延，不写正文。",
            "归档每天限制 30 分钟，剩余时间继续写草稿。",
            "最后希望 AyunOS 能像第二大脑一样帮我找回写作线索。",
            "这正适合原文回溯：先搜索角色或世界观，再回到完整对话和草稿上下文。",
            "这次归档结论是什么？",
            "结论：建立角色卡、世界观索引、章节草稿版本和审阅标签，先处理前三章。",
        ],
    },
]

SHORT_MOCK_TITLES = [
    "Mock Project Planning",
    "Mock Study Notes",
    "Mock Writing Archive",
    "Mock Debug Session",
    "Mock Product Ideas",
]

SHORT_MOCK_TEXTS = [
    "AyunOS synthetic note for a local memory workflow. This sample mentions second brain, search, tag, and review without using real records.",
    "A synthetic study item for testing local memory search. It uses only mock content and keeps private source material out of the test database.",
    "A synthetic writing archive message about organizing drafts, tags, and review status inside AyunOS.",
    "A synthetic debug session message for checking the browser detail view, local memory, and review workflow.",
    "A synthetic product idea message for testing search, tag, review, and second brain navigation.",
]

MARKS_SQL = """
CREATE TABLE IF NOT EXISTS conversation_tags (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id TEXT NOT NULL,
    tag TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS conversation_marks (
    conversation_id TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    note TEXT,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS message_marks (
    message_id TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    note TEXT,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_conversation_tags_conversation_id
    ON conversation_tags(conversation_id);
CREATE INDEX IF NOT EXISTS idx_conversation_tags_tag
    ON conversation_tags(tag);
CREATE INDEX IF NOT EXISTS idx_conversation_marks_status
    ON conversation_marks(status);
CREATE INDEX IF NOT EXISTS idx_message_marks_status
    ON message_marks(status);
"""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Create a sanitized AyunOS test memory database.")
    parser.add_argument("--source-db", default=str(REAL_DB_PATH), help="Read-only source database.")
    parser.add_argument("--out-db", default=str(TEST_DB_PATH), help="Output test database.")
    parser.add_argument("--limit-conversations", type=int, default=30, help="Maximum mock conversations.")
    parser.add_argument("--limit-messages", type=int, default=8, help="Maximum mock messages per conversation.")
    parser.add_argument("--overwrite", action="store_true", help="Overwrite the existing test database.")
    return parser.parse_args()


def table_sql(conn: sqlite3.Connection, name: str) -> str | None:
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name = ?",
        (name,),
    ).fetchone()
    return row[0] if row and row[0] else None


def columns(conn: sqlite3.Connection, name: str) -> list[str]:
    return [row[1] for row in conn.execute(f"PRAGMA table_info({name})")]


def create_source_compatible_tables(source: sqlite3.Connection, target: sqlite3.Connection) -> None:
    for name in ("conversations", "messages", "assets"):
        sql = table_sql(source, name)
        if sql:
            target.execute(sql)
    for table in ("conversations", "messages"):
        if table_sql(target, table) and "source_platform" not in columns(target, table):
            target.execute(f"ALTER TABLE {table} ADD COLUMN source_platform TEXT DEFAULT 'chatgpt'")
        if table_sql(target, table) and "memory_space" not in columns(target, table):
            target.execute(f"ALTER TABLE {table} ADD COLUMN memory_space TEXT DEFAULT 'general'")
    target.executescript(MARKS_SQL)


def iso_for(index: int, ordinal: int = 0) -> tuple[float, str]:
    dt = datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(days=index, minutes=ordinal * 7)
    return dt.timestamp(), dt.isoformat(timespec="seconds")


def fit_row(cols: list[str], values: dict) -> dict:
    return {column: values.get(column) for column in cols}


def insert_rows(conn: sqlite3.Connection, table: str, rows: list[dict]) -> None:
    if not rows:
        return
    cols = list(rows[0].keys())
    placeholders = ",".join(["?"] * len(cols))
    conn.executemany(
        f"INSERT INTO {table} ({','.join(cols)}) VALUES ({placeholders})",
        [[row.get(col) for col in cols] for row in rows],
    )


def build_mock_rows(
    source: sqlite3.Connection,
    limit_conversations: int,
    limit_messages: int,
) -> tuple[list[dict], list[dict]]:
    conv_cols = columns(source, "conversations")
    msg_cols = columns(source, "messages")
    if "source_platform" not in conv_cols:
        conv_cols.append("source_platform")
    if "source_platform" not in msg_cols:
        msg_cols.append("source_platform")
    if "memory_space" not in conv_cols:
        conv_cols.append("memory_space")
    if "memory_space" not in msg_cols:
        msg_cols.append("memory_space")

    conversations: list[dict] = []
    messages: list[dict] = []

    def add_conversation(conv_index: int, title: str, scenario_messages: list[str], tag: str) -> None:
        conv_id = f"mock-conv-{conv_index:04d}"
        message_count = len(scenario_messages)
        create_time, create_iso = iso_for(conv_index)
        update_time, update_iso = iso_for(conv_index, message_count)
        source_platform = source_platform_for(conv_index)
        memory_space = "whale" if source_platform == "deepseek" else "general"

        conversations.append(
            fit_row(
                conv_cols,
                {
                    "conversation_id": conv_id,
                    "title": title,
                    "create_time": create_time,
                    "create_time_iso": create_iso,
                    "update_time": update_time,
                    "update_time_iso": update_iso,
                    "source_file": "mock-export.json",
                    "default_model_slug": "mock-model",
                    "is_archived": "False",
                    "is_starred": "False",
                    "message_count": message_count,
                    "user_message_count": (message_count + 1) // 2,
                    "assistant_message_count": message_count // 2,
                    "char_count": sum(len(text) for text in scenario_messages),
                    "tags": tag,
                    "importance_score": 10 + (conv_index % 5),
                    "source_platform": source_platform,
                    "memory_space": memory_space,
                    "metadata_json": json.dumps({"memory_space": memory_space}, ensure_ascii=False),
                },
            )
        )

        for ordinal, text in enumerate(scenario_messages):
            message_id = f"mock-msg-{conv_index:04d}-{ordinal + 1:04d}"
            node_id = f"mock-node-{conv_index:04d}-{ordinal + 1:04d}"
            role = "user" if ordinal % 2 == 0 else "assistant"
            msg_time, msg_iso = iso_for(conv_index, ordinal)
            messages.append(
                fit_row(
                    msg_cols,
                    {
                        "message_id": message_id,
                        "node_id": node_id,
                        "conversation_id": conv_id,
                        "conversation_title": title,
                        "ordinal": ordinal,
                        "parent_id": None if ordinal == 0 else f"mock-node-{conv_index:04d}-{ordinal:04d}",
                        "children_json": json.dumps([], ensure_ascii=False),
                        "role": role,
                        "author_name": "",
                        "create_time": msg_time,
                        "create_time_iso": msg_iso,
                        "update_time": msg_time,
                        "update_time_iso": msg_iso,
                        "content_type": "text",
                        "text": f"{text} [synthetic mock]",
                        "status": "finished_successfully",
                        "weight": 1.0,
                        "metadata_json": json.dumps({"memory_space": memory_space}, ensure_ascii=False),
                        "source_platform": source_platform,
                        "memory_space": memory_space,
                    },
                )
            )

    for conv_index, scenario in enumerate(LONG_SCENARIOS, start=1):
        add_conversation(
            conv_index=conv_index,
            title=scenario["title"],
            scenario_messages=scenario["messages"],
            tag=scenario["tags"][0],
        )

    short_count = max(0, max(1, limit_conversations) - len(LONG_SCENARIOS))
    short_message_count = max(1, min(max(1, limit_messages), 8))
    for offset in range(short_count):
        conv_index = len(LONG_SCENARIOS) + offset + 1
        title_base = SHORT_MOCK_TITLES[offset % len(SHORT_MOCK_TITLES)]
        title = f"{title_base} {offset + 1:02d}"
        scenario_messages = [
            (
                f"{SHORT_MOCK_TEXTS[(offset + ordinal) % len(SHORT_MOCK_TEXTS)]} "
                f"Item {offset + 1:02d}.{ordinal + 1:02d} is synthetic."
            )
            for ordinal in range(short_message_count)
        ]
        tag = ["project", "study", "writing", "tech", "review"][offset % 5]
        add_conversation(conv_index, title, scenario_messages, tag)
    return conversations, messages


def source_platform_for(conv_index: int) -> str:
    if conv_index in {4, 9, 14}:
        return "deepseek"
    if conv_index in {5, 12}:
        return "manual"
    return "chatgpt"


def seed_marks(conn: sqlite3.Connection) -> None:
    now = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
    tag_rows = []
    mark_rows = []
    for conv_index, scenario in enumerate(LONG_SCENARIOS, start=1):
        conv_id = f"mock-conv-{conv_index:04d}"
        tag_rows.extend((conv_id, tag, now) for tag in scenario["tags"])
        mark_rows.append((conv_id, scenario["status"], scenario["note"], now))
    tag_rows.extend(
        [
            ("mock-conv-0006", "project", now),
            ("mock-conv-0007", "review", now),
        ]
    )
    conn.executemany(
        "INSERT INTO conversation_tags (conversation_id, tag, created_at) VALUES (?, ?, ?)",
        tag_rows,
    )
    conn.executemany(
        """
        INSERT INTO conversation_marks (conversation_id, status, note, updated_at)
        VALUES (?, ?, ?, ?)
        """,
        mark_rows,
    )


def main() -> int:
    args = parse_args()
    source_db = Path(args.source_db).expanduser().resolve()
    out_db = Path(args.out_db).expanduser().resolve()

    if not source_db.is_file():
        raise FileNotFoundError(f"Source database not found: {source_db}")
    if out_db.exists() and not args.overwrite:
        raise FileExistsError(f"Test database already exists: {out_db}. Use --overwrite to rebuild it.")

    out_db.parent.mkdir(parents=True, exist_ok=True)
    if out_db.exists():
        out_db.unlink()

    with sqlite3.connect(f"file:{source_db.as_posix()}?mode=ro", uri=True) as source:
        source.row_factory = sqlite3.Row
        with sqlite3.connect(out_db) as target:
            create_source_compatible_tables(source, target)
            conversations, messages = build_mock_rows(
                source,
                limit_conversations=max(1, args.limit_conversations),
                limit_messages=max(1, args.limit_messages),
            )
            insert_rows(target, "conversations", conversations)
            insert_rows(target, "messages", messages)
            seed_marks(target)
            target.commit()

    print(f"Test database created: {out_db}")
    print(f"Mock conversations: {len(conversations)}")
    print(f"Mock messages: {len(messages)}")
    print("Source database was read-only and left unchanged.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
