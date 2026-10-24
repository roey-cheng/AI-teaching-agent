"""Read the bundled teaching skill; virtual paths never become host file paths."""

from importlib.resources import files

SKILLS_ROOT = "/skills/"
TEACHING_SKILL_DIR = "/skills/adaptive-teaching"
TEACHING_SKILL_PATH = f"{TEACHING_SKILL_DIR}/SKILL.md"


def load_teaching_skill() -> str:
    return (
        files("app.agent")
        .joinpath("skills", "adaptive-teaching", "SKILL.md")
        .read_text(encoding="utf-8")
    )
