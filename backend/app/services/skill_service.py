"""发现、配置并向 Agent 暴露 PolyStudio Skills。

本服务只负责 Skill 的“索引层”，主要流程是：

1. 扫描 ``backend/skills/public`` 和 ``backend/skills/custom`` 的直接子目录；
2. 读取每个 ``SKILL.md`` 顶部的 YAML frontmatter，得到名称和用途描述；
3. 合并 ``settings.json`` 中的启用状态；
4. 只把启用 Skill 的元数据注入 System Prompt。

完整 ``SKILL.md`` 不在这里加载到 Prompt。LLM 判断某个 Skill 与用户意图匹配后，
会调用 ``read_skill_file`` Tool 按需读取正文，这就是 Progressive Loading。
"""
import json
import logging
import os
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml

logger = logging.getLogger(__name__)

# skill_service.py 位于 backend/app/services，向上三级得到 backend 根目录。
BASE_DIR = Path(__file__).parent.parent.parent  # backend/
# Skill 内容与运行时设置分开放置：skills 存定义，storage 存用户启用状态。
SKILLS_DIR = BASE_DIR / "skills"
STORAGE_DIR = BASE_DIR / "storage"
SETTINGS_FILE = STORAGE_DIR / "settings.json"


# dataclass 自动生成 __init__、__repr__ 等样板方法，适合承载扫描得到的元数据。
@dataclass
class SkillMeta:
    """从一个合法 SKILL.md 提取出的静态元数据。"""

    id: str           # 目录名，如 "xiaohongshu-copywriter"
    name: str         # frontmatter name 字段
    description: str  # frontmatter description 字段
    source: str       # "public" | "custom"
    # skill_dir 指向整个 Skill 目录，md_path 精确指向入口文档。
    skill_dir: Path
    md_path: Path


@dataclass
class SkillWithState(SkillMeta):
    """在静态 SkillMeta 基础上增加当前是否启用。"""

    # 继承 SkillMeta 的全部字段，因此构造时仍需传入 id/name/description 等。
    enabled: bool = False


# SKILL.md 约定以两个 --- 包围 YAML frontmatter，后面才是 Markdown 正文。
# re.DOTALL 让正则中的 . 也能跨越换行；两个捕获组分别得到 YAML 和正文。
_FM_PATTERN = re.compile(r"^---\s*\n(.*?)\n---\s*\n(.*)", re.DOTALL)


def _parse_skill_md(md_path: Path) -> Optional[tuple[dict, str]]:
    """
    解析 SKILL.md，返回 (frontmatter_dict, body_str)。
    解析失败时返回 None。
    """
    try:
        # Skill 文档统一按 UTF-8 读取，以保留中文名称、描述和工作流内容。
        content = md_path.read_text(encoding="utf-8")
        m = _FM_PATTERN.match(content)
        if not m:
            # 没有规范 frontmatter 时无法知道 Skill 用途，因此不加入可用列表。
            logger.warning(f"SKILL.md missing frontmatter, skipping: {md_path}")
            return None
        fm_raw, body = m.group(1), m.group(2)
        # safe_load 只解析普通 YAML 数据，不构造任意 Python 对象。
        fm = yaml.safe_load(fm_raw)
        if not isinstance(fm, dict):
            logger.warning(f"SKILL.md frontmatter is not a dict, skipping: {md_path}")
            return None
        # 当前扫描阶段只使用 fm，但同时返回 body，方便调用方未来复用解析结果。
        return fm, body
    except Exception as e:
        logger.warning(f"Failed to parse SKILL.md {md_path}: {e}")
        return None


def scan_available_skills() -> list[SkillMeta]:
    """扫描 public/custom 的直接子目录并返回所有合法 Skill 元数据。"""
    skills: list[SkillMeta] = []

    # source 会被保存进 SkillMeta，后面据此应用不同的启用策略。
    for source in ("public", "custom"):
        source_dir = SKILLS_DIR / source
        if not source_dir.exists():
            # 某类目录不存在时视为空集合，不影响另一类 Skill 使用。
            continue
        # sorted 保证扫描和 Prompt 中 Skill 的顺序稳定，便于调试和比较。
        for skill_dir in sorted(source_dir.iterdir()):
            if not skill_dir.is_dir():
                continue
            # 每个一级子目录必须以 SKILL.md 作为入口。
            md_path = skill_dir / "SKILL.md"
            if not md_path.exists():
                continue
            parsed = _parse_skill_md(md_path)
            if parsed is None:
                continue
            fm, _ = parsed
            # name 缺失时用目录名降级；description 缺失时保留为空字符串。
            name = fm.get("name", skill_dir.name)
            description = fm.get("description", "")
            skills.append(SkillMeta(
                id=skill_dir.name,
                name=name,
                description=description,
                source=source,
                skill_dir=skill_dir,
                md_path=md_path,
            ))

    return skills


def _load_settings() -> dict:
    """读取 settings.json，不存在时返回空 dict。"""
    try:
        if SETTINGS_FILE.exists():
            with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
                # JSON 对象被解析成 dict，其中 installedSkills 保存 custom Skill 开关。
                return json.load(f)
    except Exception as e:
        # 文件损坏或读取失败时使用默认空设置，让 public Skill 仍能正常工作。
        logger.warning(f"Failed to load settings.json: {e}")
    return {}


def _save_settings_atomic(data: dict) -> None:
    """通过“同目录临时文件 + os.replace”原子更新 settings.json。"""

    STORAGE_DIR.mkdir(parents=True, exist_ok=True)
    # mkstemp 原子创建临时文件，并同时返回操作系统文件描述符和路径。
    # 临时文件放在同一目录，便于 os.replace 在同一文件系统内原子替换。
    fd, tmp_path = tempfile.mkstemp(dir=STORAGE_DIR, suffix=".tmp")
    try:
        # fdopen 把底层文件描述符包装成 Python 文本文件对象，with 结束后自动关闭。
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        # 只有完整 JSON 成功写入后才替换正式文件，降低中途崩溃造成文件损坏的风险。
        os.replace(tmp_path, SETTINGS_FILE)
    except Exception:
        # 写入或替换失败时尽力清理临时文件，并把原异常继续交给调用方。
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def get_skills_with_state() -> list[SkillWithState]:
    """返回所有可用 skill 及其启用状态。

    public/ 下的 skill 默认始终启用（无需手动安装）。
    custom/ 下的 skill 从 settings.json 的 installedSkills key 读取启用状态。
    """
    # 每次调用都重新扫描，因此新增或修改 Skill 后不需要维护额外内存缓存。
    available = scan_available_skills()
    # settings.json 不存在或没有 installedSkills 时，以空映射作为默认值。
    installed: dict[str, bool] = _load_settings().get("installedSkills", {})

    result: list[SkillWithState] = []
    for meta in available:
        # public 是随系统分发的基础能力，始终启用；custom 必须显式开启。
        if meta.source == "public":
            enabled = True
        else:
            enabled = installed.get(meta.id, False)
        # 创建新对象，不修改前面扫描得到的 SkillMeta。
        result.append(SkillWithState(
            id=meta.id,
            name=meta.name,
            description=meta.description,
            source=meta.source,
            skill_dir=meta.skill_dir,
            md_path=meta.md_path,
            enabled=enabled,
        ))
    return result


def set_skill_enabled(skill_id: str, enabled: bool) -> None:
    """
    将指定 skill 的启用状态写入 settings.json 的 installedSkills key。
    使用 os.replace 原子写，防止 JSON 损坏。
    """
    # 先读取整个设置对象，只修改 installedSkills，保留 MCP、工具等其他配置键。
    data = _load_settings()
    installed: dict = data.get("installedSkills", {})
    # 这里记录 false 而不是删除键，使用户选择在 JSON 中保持明确。
    installed[skill_id] = enabled
    data["installedSkills"] = installed
    _save_settings_atomic(data)


def get_skills_context() -> str:
    """
    构建注入 system prompt 的 skill 元数据块（Progressive Loading 模式）。

    只注入 name + description + SKILL.md 文件路径，不读取文件内容。
    Agent 在判断用户意图匹配某个 skill 后，自行调用 read_skill_file 工具按需加载。
    无启用 skill 时返回 ""。
    """
    # 先合并静态扫描结果和运行时开关，再过滤出真正暴露给 Agent 的 Skill。
    skills = get_skills_with_state()
    enabled_skills = [s for s in skills if s.enabled]
    if not enabled_skills:
        return ""

    # 每项只包含路由决策所需的最小元数据，不包含可能很长的 Markdown 正文。
    items = []
    for s in enabled_skills:
        items.append(
            f"  <skill>\n"
            f"    <name>{s.name}</name>\n"
            f"    <description>{s.description}</description>\n"
            f"    <location>{s.md_path}</location>\n"
            f"  </skill>"
        )
    # XML 风格标签便于 LLM 区分多个 Skill 及各字段边界。
    skills_block = "<available_skills>\n" + "\n".join(items) + "\n</available_skills>"

    # 除了元数据，还注入选择和加载规则，要求模型先判断匹配度，再调用读取 Tool。
    return f"""<skill_system>
你可以访问以下专项 Skill，每个 Skill 提供特定领域的优化工作流和专业知识。

**Progressive Loading 使用规则：**
1. 仔细对比用户意图与每个 skill 的 description，只有高度匹配时才加载对应 skill；
2. 确认匹配后，先用简短中文向用户说明你打算加载哪个 skill 及原因；
3. 然后调用 read_skill_file 工具读取该 skill 的 <location> 路径（即 SKILL.md 文件）；
4. 仔细阅读 skill 内容，按其中定义的工作流拆解任务、逐步执行；
5. 如需更多资源，可调用 list_skill_dir 查看 skill 目录，再用 read_skill_file 加载 references/、scripts/ 等子资源；
6. 若无任何 skill 与用户需求明确匹配，直接正常回答，严禁强行关联或模糊匹配。

{skills_block}
</skill_system>"""
