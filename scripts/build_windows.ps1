param([string]$Python='python',[string]$Output='dist')
$ErrorActionPreference='Stop'
$project=Split-Path -Parent $PSScriptRoot
$resolvedOutput=[System.IO.Path]::GetFullPath((Join-Path $project $Output))
& $Python -m PyInstaller --noconfirm --clean --onedir --console --name StudyFlow `
  --paths (Join-Path $project 'desktop') --paths (Join-Path $project 'src') `
  --collect-all playwright --collect-submodules chaoxing_learning_mcp --collect-submodules mcp `
  --add-data ((Join-Path $project 'src/chaoxing_learning_mcp/assets')+';chaoxing_learning_mcp/assets') `
  --distpath $resolvedOutput --workpath (Join-Path $project 'work/pyinstaller') `
  --specpath (Join-Path $project 'work/pyinstaller') (Join-Path $PSScriptRoot 'desktop_entry.py')
if($LASTEXITCODE -ne 0){throw 'Portable build failed'}
# Qt uses Windows' ICU API. An unrelated ICU DLL on the build host PATH
# (e.g. Poppler) exports a different ABI and must not shadow System32/icuuc.dll.
foreach($name in @('icuuc.dll','icudt78.dll')){
  $foreignIcu=Join-Path $resolvedOutput ('StudyFlow/_internal/'+$name)
  if(Test-Path -LiteralPath $foreignIcu){Remove-Item -LiteralPath $foreignIcu}
}
& $Python (Join-Path $PSScriptRoot 'prepare_portable.py') (Join-Path $resolvedOutput 'StudyFlow')
if($LASTEXITCODE -ne 0){throw 'Portable documentation preparation failed'}
