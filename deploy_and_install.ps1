# deploy_and_install.ps1

# --- Configuration ---
Write-Host "=== Configuration ===" -ForegroundColor Cyan
$RemoteUserInput = Read-Host "Enter Remote User [default: e176097]"
$RemoteUser = if ([string]::IsNullOrWhiteSpace($RemoteUserInput)) { "e176097" } else { $RemoteUserInput }

$HostSuffix = Read-Host "Enter Host Suffix (e.g., '03' for hde2stl020003)"
if ([string]::IsNullOrWhiteSpace($HostSuffix)) {
    Write-Error "Host Suffix is required."
    exit 1
}

$RemoteServer = "hde2stl0200${HostSuffix}.mastercard.int"
$RemotePort = 2222
$RemotePath = "/ads_storage/dispatch"
$ZipName = "dispatch_deploy.zip"
$SetupScript = "install.sh"
$UpdateScript = "update.sh"
$BundleDir = "dependency_bundle"
$WheelDir = Join-Path $BundleDir "wheels"
$BundleRequirementsDir = Join-Path $BundleDir "requirements"
$PythonRemote = "python3" # Uses whatever python3 is on the PATH (3.10 or 3.11)

# --- Step 1: Create Verified Dependency Bundle ---
Write-Host "`n=== Step 1: Creating Verified Dependency Bundle ===" -ForegroundColor Cyan

Remove-Item -LiteralPath $BundleDir -Force -Recurse -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Path $WheelDir, $BundleRequirementsDir | Out-Null

if (!(Test-Path requirements.txt)) {
    Write-Error "requirements.txt not found!"
    exit 1
}

Write-Host "Downloading binary packages for Linux (Python 3.10)..."
py -m pip download -r requirements.txt --dest $WheelDir --platform manylinux2014_x86_64 --python-version 3.10 --implementation cp --abi cp310 --only-binary=:all:

if ($LASTEXITCODE -ne 0) {
    Write-Error "Download failed."
    exit 1
}

# Verify files exist
if ((Get-ChildItem $WheelDir).Count -eq 0) {
    Write-Error "Dependency bundle wheel directory is empty."
    exit 1
}

$ManifestScript = @"
import hashlib
import json
import subprocess
from pathlib import Path

bundle = Path(r'$BundleDir')
requirements = Path('requirements.txt').read_bytes().replace(b'\r\n', b'\n').replace(b'\r', b'\n')
(bundle / 'requirements' / 'requirements.txt').write_bytes(requirements)
files = []
for path in sorted((bundle / 'requirements').iterdir()):
    content = path.read_bytes()
    files.append({'path': f'requirements/{path.name}', 'sha256': hashlib.sha256(content).hexdigest(), 'size': len(content), 'kind': 'dependency'})
for path in sorted((bundle / 'wheels').iterdir()):
    content = path.read_bytes()
    files.append({'path': f'wheels/{path.name}', 'sha256': hashlib.sha256(content).hexdigest(), 'size': len(content), 'kind': 'wheel'})
identity = {
    'schema': 'edge-deploy/dependency-bundle/2',
    'tool': 'robocop',
    'source_sha': subprocess.run(['git', 'rev-parse', 'HEAD'], check=True, capture_output=True, text=True).stdout.strip(),
    'target': {
        'python': '3.10',
        'implementation': 'cp',
        'abi': 'cp310',
        'compatible_platform_tags': ['manylinux_2_24_x86_64', 'manylinux2014_x86_64'],
    },
    'files': sorted(files, key=lambda item: item['path']),
}
canonical = (json.dumps(identity, sort_keys=True, separators=(',', ':')) + '\n').encode()
manifest = {**identity, 'bundle_digest': hashlib.sha256(canonical).hexdigest()}
(bundle / 'manifest.json').write_text(json.dumps(manifest, indent=2, sort_keys=True) + '\n', encoding='utf-8')
"@
py -c $ManifestScript
if ($LASTEXITCODE -ne 0) {
    Write-Error "Dependency bundle manifest generation failed."
    exit 1
}

# --- Step 2: Compress Artifacts ---
Write-Host "`n=== Step 2: Compressing Artifacts (Python) ===" -ForegroundColor Cyan
if (Test-Path $ZipName) { Remove-Item $ZipName -Force }

# We use Python to zip because Compress-Archive uses Windows backslashes,
# which breaks directory structure when unzipped on Linux.
$PyScript = @"
import zipfile, os

zip_name = '$ZipName'
items = ['dispatch', 'scr', 'bin', 'dependency_bundle', 'install.sh', 'onboard.sh', 'shared_runtime.py', 'update.sh', 'pyproject.toml', 'requirements.txt', 'VERSION', 'README.md', 'docs']

print(f'Creating {zip_name}...')
with zipfile.ZipFile(zip_name, 'w', zipfile.ZIP_DEFLATED) as zf:
    for item in items:
        if not os.path.exists(item):
            print(f'  Warning: {item} not found, skipping.')
            continue
        if os.path.isfile(item):
            print(f'  Adding {item}')
            zf.write(item, os.path.basename(item))
        elif os.path.isdir(item):
            print(f'  Adding {item} (recursive)')
            for root, dirs, files in os.walk(item):
                # Skip .pyc files or __pycache__
                if '__pycache__' in root:
                    continue
                for file in files:
                    if file.endswith('.pyc'):
                        continue
                    file_path = os.path.join(root, file)
                    # Create relative path for archive
                    arcname = os.path.relpath(file_path, os.getcwd())
                    # FORCE forward slashes for Linux compatibility
                    arcname = arcname.replace(os.sep, '/')
                    zf.write(file_path, arcname)
print('Compression complete.')
"@

# Run the python script
py -c $PyScript

if ($LASTEXITCODE -ne 0) {
    Write-Error "Compression failed."
    exit 1
}

# --- Step 3: Transfer to Server ---
Write-Host "`n=== Step 3: Transferring to Server ===" -ForegroundColor Cyan
$Destination = "${RemoteUser}@${RemoteServer}:${RemotePath}"
Write-Host "Uploading $ZipName to $Destination on port $RemotePort..."

# Ensure remote directory exists
ssh -p $RemotePort "${RemoteUser}@${RemoteServer}" "mkdir -p $RemotePath"

# SCP the file
scp -P $RemotePort $ZipName "$Destination/$ZipName"

if ($LASTEXITCODE -ne 0) {
    Write-Error "Transfer failed."
    exit 1
}

# --- Step 4: Remote Installation ---
Write-Host "`n=== Step 4: Remote Installation ===" -ForegroundColor Cyan

# We add 'ls -F' to debug if the folder was created correctly
$RemoteCommand = "cd $RemotePath && " +
                 "echo '--- Unzipping artifacts ---' && " +
                 "$PythonRemote -m zipfile -e $ZipName . && " +
                 "echo '--- Verifying extraction ---' && " +
                 "ls -F && " +
                 "echo '--- Running Setup ---' && " +
                 "chmod +x $SetupScript $UpdateScript onboard.sh bin/dispatch && chmod a+r bin/runtime_check.sh && " +
                 "EDGE_DEPLOY_BUNDLE_DIR=$RemotePath/dependency_bundle DISPATCH_PYTHON_BIN=`$(command -v python3.11 || command -v python3.10) ./$SetupScript"

Write-Host "Executing setup on remote server..."
ssh -p $RemotePort "${RemoteUser}@${RemoteServer}" "$RemoteCommand"

if ($LASTEXITCODE -eq 0) {
    Write-Host "`n=== SUCCESS! ===" -ForegroundColor Green
    Write-Host "Dispatch is deployed and the shared runtime is active."
    Write-Host "Verify with: ssh -p $RemotePort ${RemoteUser}@${RemoteServer} '/ads_storage/dispatch/bin/dispatch --help'"
    Write-Host "Then each analyst runs: /ads_storage/dispatch/onboard.sh"
} else {
    Write-Error "Remote installation failed."
}
