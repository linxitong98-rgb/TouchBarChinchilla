"""Publish a byte-verified 1.13.1 installer using the public previous release."""
import bz2
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

REPO='linxitong98-rgb/TouchBarChinchilla'
VERSION='1.13.1'
EXPECTED_SHA='c3f3b2ecadebd0bcbfad393f357c3678c549ea706220574ad254e42568c7d676'
BASE_SHA='3f90b0de017144bcff79605c32ef8b2366f9adba7e6c65230b54cf0009a338dc'


def api(path,payload=None,method='GET'):
    command=['gh','api',path]
    if payload is not None:command += ['--method',method,'--input','-']
    result=subprocess.run(command,input=json.dumps(payload) if payload is not None else None,capture_output=True,text=True,check=True)
    return json.loads(result.stdout)


def download(asset,target):
    with target.open('wb') as file:
        subprocess.run(['gh','api','-H','Accept: application/octet-stream',f'repos/{REPO}/releases/assets/{asset["id"]}'],stdout=file,check=True)
def off_t(data):
    assert len(data) == 8
    value = int.from_bytes(data, 'little')
    return -(value & ((1 << 63) - 1)) if value >> 63 else value


def apply_bsdiff(old, patch, expected_size):
    assert patch[:8] == b'BSDIFF40'
    ctrl_size, diff_size, new_size = [off_t(patch[i:i+8]) for i in (8, 16, 24)]
    assert ctrl_size >= 0 and diff_size >= 0 and new_size == expected_size
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


def reconstruct(root,manifest,base_path):
    assert manifest['version']==VERSION and manifest['sha256']==EXPECTED_SHA
    assert manifest['base']['sha256']==BASE_SHA and manifest['base']['tag']=='v1.12.2'
    old=base_path.read_bytes()
    pieces=[]
    for part in manifest['patch']['parts']:
        data=(root/part['name']).read_bytes()
        assert len(data)==part['size'] and hashlib.sha256(data).hexdigest()==part['sha256']
        pieces.append(data)
    patch=b''.join(pieces)
    assert len(old)==manifest['base']['size'] and hashlib.sha256(old).hexdigest()==BASE_SHA
    assert len(patch)==manifest['patch']['size'] and hashlib.sha256(patch).hexdigest()==manifest['patch']['sha256']
    data=apply_bsdiff(old,patch,manifest['size'])
    assert len(data)==manifest['size'] and hashlib.sha256(data).hexdigest()==EXPECTED_SHA
    output=Path('dist');output.mkdir(exist_ok=True)
    installer=output/manifest['filename'];installer.write_bytes(data)
    shutil.copyfile(root/'User-Guide.md',output/'User-Guide.md')
    (output/'SHA256.txt').write_text(EXPECTED_SHA+'  '+installer.name+'\n')
    print('Restored and verified:',installer.name,len(data),EXPECTED_SHA)
    return installer


if __name__=='__main__':
    if len(sys.argv)==4 and sys.argv[1]=='--verify-local':
        root=Path(sys.argv[2]);manifest=json.loads((root/'upload-manifest.json').read_text())
        reconstruct(root,manifest,Path(sys.argv[3]));sys.exit(0)
    assert os.environ['GH_REPO']==REPO
    root=Path('distribution/v'+VERSION)
    manifest=json.loads((root/'upload-manifest.json').read_text())
    releases=api(f'repos/{REPO}/releases')
    base=next(r for r in releases if r['tag_name']==manifest['base']['tag'] and not r['draft'])
    base_asset=next(a for a in base['assets'] if a['name']==manifest['base']['filename'])
    assert base_asset['size']==manifest['base']['size']
    staging=next(r for r in releases if r['draft'] and r['name']==manifest['staging_title'])
    download(base_asset,Path('base.dmg'))
    for part in manifest['patch']['parts']:
        patch_asset=next(a for a in staging['assets'] if a['name']==part['name'])
        assert patch_asset['size']==part['size']
        download(patch_asset,root/part['name'])
    installer=reconstruct(root,manifest,Path('base.dmg'))
    tag='v'+VERSION;title='Touch Bar 小龙猫 '+VERSION
    notes=(root/'RELEASE-NOTES.md').read_text()
    release=next((r for r in releases if r['tag_name']==tag),None)
    if release and not release['draft']:
        previous=next((a for a in release['assets'] if a['name']==installer.name),None)
        if previous and previous.get('digest') and previous['digest']!='sha256:'+EXPECTED_SHA:
            raise RuntimeError('Published version contains a different installer; refusing to replace it')
    metadata={'tag_name':tag,'target_commitish':os.environ['GITHUB_SHA'],'name':title,'body':notes}
    if release is None:release=api(f'repos/{REPO}/releases',{**metadata,'draft':True},'POST')
    else:release=api(f'repos/{REPO}/releases/{release["id"]}',metadata,'PATCH')
    subprocess.run(['gh','release','upload',tag,str(installer),'dist/User-Guide.md','dist/SHA256.txt','--clobber'],check=True)
    release=api(f'repos/{REPO}/releases/{release["id"]}',{'draft':False,'make_latest':'true'},'PATCH')
    asset=next(a for a in release['assets'] if a['name']==installer.name)
    assert asset['size']==manifest['size']
    if asset.get('digest'):assert asset['digest']=='sha256:'+EXPECTED_SHA
    print('Published:',release['html_url'])
