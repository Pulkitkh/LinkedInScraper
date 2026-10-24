"""Template for a client-specific resume customizer (like safi_customizer.py / tanuja_customizer.py).

Use this when a client's resume layout (custom tables, unusual headings) doesn't parse cleanly with
resume_customizer.py. Copy this file to <client>_customizer.py, adapt `find_sections`, register it in
gmail_sender.CUSTOMIZERS and set CLIENTS[key]["customizer"] to its name.
"""

from __future__ import annotations

import resume_customizer as rc


def find_sections(doc) -> rc.ResumeStructure:
    """Example: this client keeps project bullets in a two-column 'Responsibilities' table."""
    structure = rc.extract_structure(doc)
    known = {id(p._p) for p in structure.paragraphs}
    for table in doc.tables:
        for row in table.rows:
            if len(row.cells) == 2 and row.cells[0].text.strip().lower().startswith("responsibilit"):
                structure.paragraphs.extend(
                    p for p in row.cells[1].paragraphs if p.text.strip() and id(p._p) not in known)
    return structure


def customize(candidate: dict, resume_path, job_description: str, role: str) -> rc.CustomizationResult:
    return rc.customize(resume_path, job_description, role, candidate, parser=find_sections)
