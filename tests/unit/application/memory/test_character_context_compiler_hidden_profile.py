"""I-02：隐藏身份必须随 T0 强制层一起交给 AI，但门控必须留在渲染方。

红线（本文件负责钉死）：
1. compile() 不得把隐藏面拼进 t0 字符串 —— 拼进去渲染方就再也剔除不掉，POV 防火墙形同虚设。
2. compile() 不得做章号判断 —— 它的结果会被烤进缓存 projection，某章的判断会被后续章节复用。
3. 隐藏面必须以独立锁字段输出，否则渲染方无门可门。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from application.memory.services.character_context_compiler import CharacterContextCompiler
from application.memory.services.character_projection_service import CharacterProjectionService
from application.memory.services.narrative_memory_service import NarrativeMemoryService
from infrastructure.persistence.database.connection import DatabaseConnection
from infrastructure.persistence.database.sqlite_memory_repository import SqliteNarrativeMemoryRepository

SECRET = "其实是潜伏的东厂暗桩"


def _projection(hidden=SECRET, reveal=20):
    return {
        "entity_id": "c1",
        "name": "沈砚",
        "constitution": {
            "public_profile": "刑部主事",
            "hidden_profile": hidden,
            "reveal_chapter": reveal,
            "core_belief": "律法高于私情",
            "moral_taboos": [],
        },
        "current_state": {},
        "voice_fingerprint": {},
        "active_scars": [],
        "active_motivations": [],
        "knowledge_boundary": {},
    }


@dataclass
class _CID:
    value: str


@dataclass
class _Char:
    id: str
    name: str
    novel_id: str = "n1"
    description: str = ""
    public_profile: str = "刑部主事"
    hidden_profile: str = SECRET
    reveal_chapter: int = 20
    core_belief: str = "律法高于私情"
    moral_taboos: list = field(default_factory=list)
    voice_profile: dict = field(default_factory=dict)
    mental_state: str = ""
    mental_state_reason: str = ""
    verbal_tic: str = ""
    idle_behavior: str = ""

    @property
    def character_id(self):
        return _CID(self.id)


class _UnifiedCharacterRepo:
    def __init__(self, chars):
        self.chars = {c.id: c for c in chars}

    def get(self, character_id):
        return self.chars.get(character_id.value)


def test_compile_emits_hidden_face_as_separate_lock_field():
    locks = CharacterContextCompiler().compile(_projection())

    assert locks["t0_hidden"] == f"隐藏面:{SECRET}"
    assert locks["t0_hidden_reveal_chapter"] == 20


def test_hidden_face_is_never_woven_into_the_t0_string():
    """红线 1：编进 t0 之后渲染方无法剔除，门控就是摆设。"""
    locks = CharacterContextCompiler().compile(_projection())

    assert SECRET not in locks["t0"]
    assert SECRET not in locks["t1"]
    assert SECRET not in locks["t2"]
    # 公开面仍要照常出现在 t0 —— 隐藏面缺席不等于公开面也缺席
    assert "刑部主事" in locks["t0"]


def test_compile_does_not_pre_gate_on_reveal_chapter():
    """红线 2：reveal_chapter 还没到的角色，隐藏面也必须照样产出。

    章号判断不在 compile() 里，否则「未来才揭示」会被烤死在缓存 projection 上。
    """
    locks = CharacterContextCompiler().compile(_projection(reveal=20))

    assert locks["t0_hidden"] == f"隐藏面:{SECRET}"
    assert locks["t0_hidden_reveal_chapter"] == 20


def test_compile_is_deterministic_for_the_same_projection():
    compiler = CharacterContextCompiler()
    projection = _projection()

    assert compiler.compile(projection) == compiler.compile(projection)


def test_absent_hidden_profile_yields_empty_hidden_field():
    locks = CharacterContextCompiler().compile(_projection(hidden="", reveal=20))

    assert locks["t0_hidden"] == ""
    # 揭示章号仍要透传，渲染方才知道「本来就没有隐藏面」而不是「门控失效」
    assert locks["t0_hidden_reveal_chapter"] == 20


def test_missing_constitution_keys_do_not_break_compile():
    locks = CharacterContextCompiler().compile({"entity_id": "c9"})

    assert locks["t0_hidden"] == ""
    assert locks["t0_hidden_reveal_chapter"] is None


def test_hidden_face_survives_the_projection_pipeline(tmp_path):
    """端到端：_constitution 取出的隐藏面要真的落到缓存 projection 的 context_locks 上。"""
    db = DatabaseConnection(str(tmp_path / "memory.db"))
    svc = NarrativeMemoryService(SqliteNarrativeMemoryRepository(db))
    db.execute("INSERT INTO novels (id, title, slug) VALUES (?, ?, ?)", ("n1", "测试小说", "n1"))
    db.get_connection().commit()

    projection = CharacterProjectionService(
        memory_service=svc,
        unified_character_repository=_UnifiedCharacterRepo([_Char("c1", "沈砚")]),
    ).get_projection("n1", "c1")

    locks = projection["context_locks"]
    assert locks["t0_hidden"] == f"隐藏面:{SECRET}"
    assert locks["t0_hidden_reveal_chapter"] == 20
    assert SECRET not in locks["t0"]


def test_cached_projection_lock_carries_no_chapter_decision(tmp_path):
    """同一份缓存 projection 拿出来两次，隐藏面锁必须一模一样（章号无关）。"""
    db = DatabaseConnection(str(tmp_path / "memory.db"))
    svc = NarrativeMemoryService(SqliteNarrativeMemoryRepository(db))
    db.execute("INSERT INTO novels (id, title, slug) VALUES (?, ?, ?)", ("n1", "测试小说", "n1"))
    db.get_connection().commit()

    service = CharacterProjectionService(
        memory_service=svc,
        unified_character_repository=_UnifiedCharacterRepo([_Char("c1", "沈砚")]),
    )
    service.rebuild_projection("n1", "c1")

    cached = service.get_projection("n1", "c1", rebuild=False)
    again = service.get_projection("n1", "c1", rebuild=False)

    assert cached["context_locks"] == again["context_locks"]
    assert cached["context_locks"]["t0_hidden"] == f"隐藏面:{SECRET}"
    assert cached["context_locks"]["t0_hidden_reveal_chapter"] == 20
