"""Restore byte-identical release media from small, verified upload segments."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess

repo = os.environ["GH_REPO"]
assert repo == "linxitong98-rgb/TouchBarChinchilla"
root = Path("distribution/v1.11.1")
manifest = json.loads((root / "manifest.json").read_text())
assert manifest["sha256"] == "a3a1c4ca83186611522f575742538518d4b1f29948834c5e2c80d1b5bbd1057c"
assert manifest["size"] == 82140062
out = Path("dist")
out.mkdir(exist_ok=True)
installer = out / "TouchBarChinchilla-1.11.1-universal.dmg"
digest = hashlib.sha256()
size = 0
with installer.open("wb") as target:
    for index, part in enumerate(manifest["parts"]):
        assert part["index"] == index
        data = (root / "parts" / f"part-{index:04d}").read_bytes()
        assert len(data) == part["size"]
        assert hashlib.sha1(b"blob " + str(len(data)).encode() + b"\0" + data).hexdigest() == part["sha"]
        target.write(data)
        digest.update(data)
        size += len(data)
assert size == manifest["size"]
assert digest.hexdigest() == manifest["sha256"]
shutil.copyfile(root / "User-Guide.md", out / "User-Guide.md")
(out / "SHA256.txt").write_text(digest.hexdigest() + "  " + installer.name + "\n")
print("Installer restored and SHA256 verified:", installer.name, size)


def api(path, payload=None):
    command = ["gh", "api", path]
    if payload is not None:
        command += ["--method", "PATCH" if "/releases/" in path else "POST", "--input", "-"]
    result = subprocess.run(command, input=json.dumps(payload) if payload is not None else None,
                            capture_output=True, text=True, check=True)
    return json.loads(result.stdout)


title = "Touch Bar 小龙猫 1.11.1"
notes = (root / "RELEASE-NOTES.md").read_text()
releases = api(f"repos/{repo}/releases")
release = next((r for r in releases if r["tag_name"] == "v1.11.1"), None)
if release is None:
    release = next((r for r in releases if r["draft"] and r["name"] == title), None)
metadata = {"tag_name": "v1.11.1", "target_commitish": os.environ["GITHUB_SHA"], "name": title, "body": notes}
if release is None:
    release = api(f"repos/{repo}/releases", {**metadata, "draft": True})
else:
    release = api(f"repos/{repo}/releases/{release['id']}", metadata)
subprocess.run(["gh", "release", "upload", "v1.11.1", str(installer), str(out / "User-Guide.md"),
                str(out / "SHA256.txt"), "--clobber"], check=True)
release = api(f"repos/{repo}/releases/{release['id']}", {"draft": False, "make_latest": "true"})
asset = next(a for a in release["assets"] if a["name"] == installer.name)
assert asset["size"] == size
if asset.get("digest"):
    assert asset["digest"] == "sha256:" + digest.hexdigest()
print("Published:", release["html_url"])
