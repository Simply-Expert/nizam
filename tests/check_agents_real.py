"""Run the resolver over the last 7 days of real transcripts and print the tree."""
import json, sys, time, collections
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from nizam.agents import resolve, display_path

root = Path.home()/".claude/projects"; cutoff = time.time()-7*86400
tree = collections.defaultdict(lambda: collections.Counter())
noagent = collections.Counter()
for d in root.iterdir():
    for f in d.glob("*.jsonl"):
        if f.stat().st_mtime < cutoff: continue
        cwd=None
        with open(f, errors="ignore") as fh:
            for line in fh:
                if '"cwd"' in line:
                    try: cwd=json.loads(line).get("cwd")
                    except: pass
                    if cwd: break
        if not cwd: continue
        agent, area = resolve(cwd)
        tree[agent.name][area.rel if area else "(root)"] += 1
for name, areas in sorted(tree.items(), key=lambda kv:-sum(kv[1].values())):
    print(f"{name}  ({sum(areas.values())} sessions)")
    for rel, n in sorted(areas.items()): print(f"   {rel:40} {n}")
print("\nNo agent (no CLAUDE.md ancestor):")
for p,n in noagent.most_common(): print(f"   {p:50} {n}")
