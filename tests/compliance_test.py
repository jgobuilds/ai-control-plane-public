#!/usr/bin/env python3
"""Conformity tests for the framework-coverage matrix.

Guards the compliance map's integrity WITHOUT touching conformance_test.py:
  - framework-map.yaml is well-formed: every mapping entry has
    control / framework / requirement / coverage, and coverage & framework are
    drawn from the allowed sets;
  - every referenced the control plane control name matches a REAL control row in
    GOVERNANCE.md (the "-" placeholder is allowed only for pure gaps, which must
    be coverage:none);
  - the `controls` catalog in the yaml matches the GOVERNANCE.md control names;
  - the generator runs clean and produces docs/compliance-map.md.

    python tests/compliance_test.py      # exits non-zero on any failure
"""
import os, re, sys, subprocess, yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MAP_PATH = os.path.join(ROOT, "compliance", "framework-map.yaml")
GOV_PATH = os.path.join(ROOT, "docs", "security", "GOVERNANCE.md")
MD_OUT = os.path.join(ROOT, "docs", "compliance-map.md")

COVERAGE_VALUES = {"full", "partial", "none"}
GAP_PLACEHOLDER = "-"

fails = []
def check(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  — {detail}" if detail and not cond else ""))
    if not cond:
        fails.append(name)


def governance_control_names(path):
    """Parse the GOVERNANCE.md control-catalog table: the FIRST bold **...** in
    the 'Control' cell (2nd column) of each numbered row is the canonical name."""
    names = set()
    row_re = re.compile(r"^\|\s*([0-9]+[a-z]?)\s*\|(.*?)\|")
    bold_re = re.compile(r"\*\*(.+?)\*\*")
    with open(path, encoding="utf-8") as f:
        for line in f:
            m = row_re.match(line)
            if not m:
                continue
            b = bold_re.search(m.group(2))
            if b:
                names.add(b.group(1).strip())
    return names


with open(MAP_PATH, encoding="utf-8") as f:
    doc = yaml.safe_load(f)

GOV_NAMES = governance_control_names(GOV_PATH)
print(f"Parsed {len(GOV_NAMES)} control names from GOVERNANCE.md")
check("GOVERNANCE.md yielded control names", len(GOV_NAMES) > 0)

frameworks = set(doc.get("frameworks", {}).keys())
print("Frameworks are declared and non-empty:")
check("at least one framework declared", len(frameworks) > 0)
for fw_key, fw_meta in doc.get("frameworks", {}).items():
    check(f"{fw_key} has a name", bool(fw_meta.get("name")))
    check(f"{fw_key} records a version/date note", bool(fw_meta.get("version_note")))

print("Every mapping entry is well-formed:")
mappings = doc.get("mappings", [])
check("mappings present", len(mappings) > 0)
for i, m in enumerate(mappings):
    tag = f"entry[{i}] ({m.get('control','?')} -> {m.get('framework','?')})"
    for field in ("control", "framework", "requirement", "how", "coverage"):
        check(f"{tag} has {field}", bool(str(m.get(field, "")).strip()))
    check(f"{tag} coverage in allowed set", m.get("coverage") in COVERAGE_VALUES,
          repr(m.get("coverage")))
    check(f"{tag} framework in declared set", m.get("framework") in frameworks,
          repr(m.get("framework")))

print("Referenced control names resolve to a real GOVERNANCE.md control:")
for i, m in enumerate(mappings):
    ctrl = m.get("control")
    if ctrl == GAP_PLACEHOLDER:
        # placeholder only permitted for genuine gaps
        check(f"entry[{i}] gap placeholder is coverage:none",
              m.get("coverage") == "none", repr(m.get("coverage")))
        continue
    check(f"entry[{i}] control '{ctrl}' matches GOVERNANCE.md", ctrl in GOV_NAMES,
          f"not in {sorted(GOV_NAMES)}")

print("The yaml `controls` catalog mirrors GOVERNANCE.md:")
catalog = doc.get("controls", [])
check("controls catalog present", len(catalog) > 0)
cat_names = {c.get("name") for c in catalog}
check("catalog names all match GOVERNANCE.md", cat_names <= GOV_NAMES,
      f"extra: {sorted(cat_names - GOV_NAMES)}")
check("catalog covers every GOVERNANCE.md control", GOV_NAMES <= cat_names,
      f"missing: {sorted(GOV_NAMES - cat_names)}")

print("Each declared framework has at least one mapping:")
for fw_key in frameworks:
    n = sum(1 for m in mappings if m.get("framework") == fw_key)
    check(f"{fw_key} has >=1 mapping", n > 0, "none")

print("Gaps section is well-formed:")
for i, g in enumerate(doc.get("gaps", [])):
    for field in ("id", "title", "note"):
        check(f"gap[{i}] has {field}", bool(str(g.get(field, "")).strip()))

print("Generator runs and produces the doc:")
try:
    os.remove(MD_OUT)
except FileNotFoundError:
    pass
proc = subprocess.run([sys.executable, os.path.join(ROOT, "scripts", "gen_compliance_map.py")],
                      capture_output=True, text=True)
check("generator exits 0", proc.returncode == 0, proc.stderr.strip())
check("docs/compliance-map.md produced",
      os.path.isfile(MD_OUT) and os.path.getsize(MD_OUT) > 0)
if os.path.isfile(MD_OUT):
    with open(MD_OUT, encoding="utf-8") as f:
        body = f.read()
    for fw_meta in doc.get("frameworks", {}).values():
        check(f"doc contains '{fw_meta['name']}' matrix", fw_meta["name"] in body)

print()
if fails:
    print(f"FAILED: {len(fails)} check(s): {', '.join(fails)}")
    sys.exit(1)
print("All compliance-map checks passed.")
