"""Restore and publish the byte-verified 1.11.2 installer."""
import base64
import bz2
from concurrent.futures import ThreadPoolExecutor
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import zipfile

REPO = 'linxitong98-rgb/TouchBarChinchilla'
NEW_SHA = '5cb6f15754359fc46dc7d6e3800524b20f86dae62e73f9955b445d51014eeb25'
BASE_SHA = 'a3a1c4ca83186611522f575742538518d4b1f29948834c5e2c80d1b5bbd1057c'


def api(path, payload=None, method='GET'):
    command = ['gh', 'api', path]
    if payload is not None:
        command += ['--method', method, '--input', '-']
    result = subprocess.run(command, input=json.dumps(payload) if payload is not None else None,
                            capture_output=True, text=True, check=True)
    return json.loads(result.stdout)


def off_t(data):
    assert len(data) == 8
    value = int.from_bytes(data, 'little')
    return -(value & ((1 << 63) - 1)) if value >> 63 else value


def apply_bsdiff(old, patch):
    assert patch[:8] == b'BSDIFF40'
    ctrl_size, diff_size, new_size = [off_t(patch[i:i+8]) for i in (8, 16, 24)]
    assert ctrl_size >= 0 and diff_size >= 0 and new_size == 82141406
    ctrl = io.BytesIO(bz2.decompress(patch[32:32+ctrl_size]))
    diff = io.BytesIO(bz2.decompress(patch[32+ctrl_size:32+ctrl_size+diff_size]))
    extra = io.BytesIO(bz2.decompress(patch[32+ctrl_size+diff_size:]))
    new = bytearray(new_size)
    old_pos = new_pos = 0
    while new_pos < new_size:
        values = [off_t(ctrl.read(8)) for _ in range(3)]
        add, copy, seek = values
        assert add >= 0 and copy >= 0
        assert new_pos + add + copy <= new_size
        block = bytearray(diff.read(add)); assert len(block) == add
        begin, end = max(0, -old_pos), min(add, len(old)-old_pos)
        for i in range(begin, end):
            block[i] = (block[i] + old[old_pos+i]) & 255
        new[new_pos:new_pos+add] = block
        new_pos += add; old_pos += add
        block = extra.read(copy); assert len(block) == copy
        new[new_pos:new_pos+copy] = block
        new_pos += copy; old_pos += seek
    return bytes(new)


def reconstruct(root, manifest, fetch_part, output):
    assert manifest['version'] == '1.11.2' and manifest['sha256'] == NEW_SHA
    assert manifest['base']['sha256'] == BASE_SHA
    bundle = bytearray()
    for segment in manifest['segments']:
        data = (root / segment['name']).read_bytes()
        assert len(data) == segment['size'] and hashlib.sha256(data).hexdigest() == segment['sha256']
        bundle += data
    assert hashlib.sha256(bundle).hexdigest() == manifest['bundle_sha256']
    with zipfile.ZipFile(io.BytesIO(bundle)) as archive:
        repairs = archive.read('base-repairs.bin')
        tail = archive.read('base-tail.bin')
        patch = archive.read('update.bsdiff')
    base = manifest['base']
    available = set(base['available'])
    repair_offsets = {p['index']: p for p in base['repairs']}
    to_fetch = [p for p in base['prefix'] if p['index'] in available]
    with ThreadPoolExecutor(max_workers=6) as pool:
        downloaded = dict(zip((p['index'] for p in to_fetch), pool.map(fetch_part, to_fetch)))
    old = bytearray()
    for part in base['prefix']:
        if part['index'] in available:
            data = downloaded[part['index']]
        else:
            record = repair_offsets[part['index']]
            data = repairs[record['offset']:record['offset']+record['size']]
        assert len(data) == part['size']
        assert hashlib.sha1(b'blob '+str(len(data)).encode()+b'\0'+data).hexdigest() == part['sha']
        old += data
    old += tail
    assert len(old) == base['size'] and hashlib.sha256(old).hexdigest() == BASE_SHA
    data = apply_bsdiff(old, patch)
    assert len(data) == manifest['size'] and hashlib.sha256(data).hexdigest() == NEW_SHA
    output.mkdir(exist_ok=True)
    installer = output / manifest['filename']
    installer.write_bytes(data)
    shutil.copyfile(root / 'User-Guide.md', output / 'User-Guide.md')
    (output / 'SHA256.txt').write_text(NEW_SHA+'  '+installer.name+'\n')
    print('Restored and verified:', installer.name, len(data), NEW_SHA)
    return installer


if __name__ == '__main__':
    if len(sys.argv) == 4 and sys.argv[1] == '--verify-local':
        root = Path(sys.argv[2]); original = Path(sys.argv[3]).read_bytes()
        manifest = json.loads((root / 'upload-manifest.json').read_text())
        reconstruct(root, manifest, lambda p: original[p['index']*262144:p['index']*262144+p['size']], root / 'verify-dist')
        sys.exit(0)
    assert os.environ['GH_REPO'] == REPO
    root = Path('distribution/v1.11.2')
    manifest = json.loads((root / 'upload-manifest.json').read_text())
    releases = api(f'repos/{REPO}/releases')
    staging = next(r for r in releases if r['draft'] and r['name'] == manifest['staging_title'])
    assets = {a['name']: a for a in staging['assets']}
    for segment in manifest['segments']:
        asset = assets[segment['name']]; assert asset['size'] == segment['size']
        with (root / segment['name']).open('wb') as target:
            subprocess.run(['gh', 'api', '-H', 'Accept: application/octet-stream',
                            f'repos/{REPO}/releases/assets/{asset["id"]}'], stdout=target, check=True)
    def fetch_part(part):
        blob = api(f'repos/{REPO}/git/blobs/{part["sha"]}')
        assert blob['encoding'] == 'base64'
        return base64.b64decode(blob['content'])
    installer = reconstruct(root, manifest, fetch_part, Path('dist'))
    title = 'Touch Bar 小龙猫 1.11.2'
    notes = (root / 'RELEASE-NOTES.md').read_text()
    release = next((r for r in releases if r['tag_name'] == 'v1.11.2'), None)
    metadata = {'tag_name':'v1.11.2','target_commitish':os.environ['GITHUB_SHA'],'name':title,'body':notes}
    if release is None:
        release = api(f'repos/{REPO}/releases', {**metadata,'draft':True}, 'POST')
    else:
        release = api(f'repos/{REPO}/releases/{release["id"]}', metadata, 'PATCH')
    subprocess.run(['gh','release','upload','v1.11.2',str(installer),'dist/User-Guide.md','dist/SHA256.txt','--clobber'],check=True)
    release = api(f'repos/{REPO}/releases/{release["id"]}', {'draft':False,'make_latest':'true'}, 'PATCH')
    asset = next(a for a in release['assets'] if a['name'] == installer.name)
    assert asset['size'] == manifest['size']
    if asset.get('digest'): assert asset['digest'] == 'sha256:'+NEW_SHA
    print('Published:', release['html_url'])
