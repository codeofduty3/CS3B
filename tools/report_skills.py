"""Measure the indexed skill context and verify the lockfile."""
import json
from pathlib import Path

from creditwatch import skills

ROOT = Path(__file__).resolve().parents[1]


def main():
    root = ROOT / "agent" / "skills"
    loaded = skills.load_skills(root)
    lockfile = json.loads((root / "skills.lock").read_text())
    skills.verify(root, lockfile)
    index = skills.skill_index(loaded)
    full_chars = sum(len(text) for text in loaded.values())
    index_chars = len(index)
    scripted = sorted(
        name for name in loaded
        if any(path.is_file() for path in (root / name / "scripts").glob("*")))
    saving = 1 - (index_chars / full_chars) if full_chars else 0
    result = {
        "skills": sorted(loaded),
        "count": len(loaded),
        "scripted": scripted,
        "index_chars": index_chars,
        "full_text_chars": full_chars,
        "index_saving_percent": round(100 * saving, 1),
        "lock_verified": True,
    }
    output = ROOT / "outputs" / "reports"
    output.mkdir(parents=True, exist_ok=True)
    (output / "skills.json").write_text(json.dumps(result, indent=2) + "\n")
    (output / "skills.md").write_text(
        "# Skill context\n\n"
        f"- Skills: {result['count']}\n"
        f"- Deterministic scripts: {len(scripted)} ({', '.join(scripted)})\n"
        f"- Full skill text: {full_chars:,} characters\n"
        f"- Index exposed in normal context: {index_chars:,} characters\n"
        f"- Index saving before on-demand loading: {100 * saving:.1f}%\n"
        "- Lockfile: verified\n")
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
