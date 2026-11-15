"""Build the minimal public ZIP from an explicit file allowlist."""
from pathlib import Path
import hashlib
import shutil
import zipfile

ROOT = Path(__file__).resolve().parents[1]
FILES = (
    'Start-YunOS.bat', 'README.md', '安装和使用教程.txt',
    'scripts/start_public.ps1', 'public_app/server.py',
    'public_app/web/index.html', 'public_app/web/style.css', 'public_app/web/app.js',
)


def main():
    output = ROOT / 'output/public-release-0.2.2'
    output.mkdir(parents=True, exist_ok=True)
    archive = output / 'YunOS-0.2.2.zip'
    with zipfile.ZipFile(archive, 'w', zipfile.ZIP_DEFLATED) as bundle:
        for name in FILES:
            path = ROOT / name
            if not path.is_file() or path.is_symlink():
                raise ValueError('Missing or linked release file: ' + name)
            bundle.write(path, name)
    shutil.copyfile(ROOT / 'Start-YunOS.bat', output / 'Start-YunOS.bat')
    lines = [hashlib.sha256((output / name).read_bytes()).hexdigest() + '  ' + name
             for name in ('YunOS-0.2.2.zip', 'Start-YunOS.bat')]
    (output / 'SHA256SUMS.txt').write_text('\n'.join(lines) + '\n', encoding='ascii')
    with zipfile.ZipFile(archive) as bundle:
        assert set(bundle.namelist()) == set(FILES)
        assert bundle.testzip() is None
    print(f'{archive}: {archive.stat().st_size} bytes, {len(FILES)} allowlisted files')


if __name__ == '__main__':
    main()
