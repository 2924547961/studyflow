"""Run after build_windows.ps1; create a self-contained PyInstaller executable."""
from pathlib import Path
import subprocess
import sys
import tomllib

root=Path(__file__).resolve().parents[1]
version=tomllib.loads((root/'pyproject.toml').read_text(encoding='utf-8'))['project']['version']
spec=(root/'work/pyinstaller/StudyFlow.spec').read_text(encoding='utf-8')
extra=[(str(root/'src/chaoxing_learning_mcp/__main__.py'),'chaoxing_learning_mcp'),
       (str(root/'dist/StudyFlow/licenses'),'licenses'),(str(root/'docs'),'docs'),
       (str(root/'LICENSE'),'.'),(str(root/'THIRD_PARTY_NOTICES.md'),'.')]
spec=spec.replace('binaries = []','datas += '+repr(extra)+'\nbinaries = []')
spec=spec[:spec.index('pyz = PYZ')]+'''
a.binaries=[row for row in a.binaries if Path(row[0]).name.lower() not in ('icuuc.dll','icudt78.dll')]
pyz=PYZ(a.pure)
exe=EXE(pyz,a.scripts,a.binaries,a.datas,[],name=OUTPUT_NAME,debug=False,
        strip=False,upx=True,console=True)
'''.replace('OUTPUT_NAME',repr('StudyFlow-'+version+'-Windows-x64'))
spec='from pathlib import Path\n'+spec
target=root/'work/pyinstaller/StudyFlow-single.spec';target.write_text(spec,encoding='utf-8')
subprocess.run([sys.executable,'-m','PyInstaller','--noconfirm','--distpath',str(root/'dist-single'),
               '--workpath',str(root/'work/onefile'),str(target)],cwd=root,check=True)
