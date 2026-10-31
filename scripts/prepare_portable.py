"""Attach source documentation and upstream license texts to an onedir build."""
from pathlib import Path
import importlib.metadata as metadata
import json
import shutil
import sys
from packaging.requirements import Requirement

root=Path(__file__).resolve().parents[1]
portable=Path(sys.argv[1]);out=portable/'licenses';out.mkdir(exist_ok=True)
queue=['PyQt6','mcp','requests','cryptography','playwright','beautifulsoup4','numpy','Pillow','pywin32','setuptools','tzdata']
seen=set();rows=[]
while queue:
    name=queue.pop();key=name.lower().replace('_','-')
    if key in seen:continue
    seen.add(key)
    try:d=metadata.distribution(name)
    except metadata.PackageNotFoundError:continue
    rows.append({'name':d.metadata['Name'],'version':d.version,
        'project_urls':d.metadata.get_all('Project-URL') or [],
        'source_package':f'https://pypi.org/project/{d.metadata["Name"]}/{d.version}/#files'})
    for file in d.files or []:
        relative=str(file).replace('\\','/')
        if '.dist-info/' not in relative or not any(s in relative.lower() for s in ('license','copying','notice')):continue
        target=out/d.metadata['Name']/relative.split('.dist-info/',1)[-1]
        source=Path(d.locate_file(file))
        if source.is_file():target.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(source,target)
    for value in d.requires or []:
        req=Requirement(value)
        if req.marker is None or req.marker.evaluate({'extra':''}):queue.append(req.name)
(out/'dependencies.json').write_text(json.dumps(rows,indent=2,ensure_ascii=False),encoding='utf-8')
for source in Path(metadata.distribution('playwright').locate_file('playwright/driver')).glob('*'):
    if source.is_file() and any(s in source.name.lower() for s in ('license','notice')):shutil.copy2(source,out/('node-'+source.name))
python_license=Path(sys.base_prefix)/'LICENSE.txt'
if python_license.exists():shutil.copy2(python_license,out/'Python-LICENSE.txt')
for name in ('LICENSE','THIRD_PARTY_NOTICES.md','README.md'):shutil.copy2(root/name,portable/name)
shutil.copytree(root/'docs',portable/'docs',dirs_exist_ok=True)
print(f'Prepared portable documentation and {len(rows)} dependency notices.')
