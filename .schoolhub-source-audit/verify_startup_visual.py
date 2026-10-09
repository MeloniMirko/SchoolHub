"""Windows-only test of the actual frozen EXE's rendered startup window."""
import json
import os
from pathlib import Path
import subprocess
import time
from PIL import ImageGrab, ImageChops

out = Path('dist/startup-visual.json').resolve()
out.unlink(missing_ok=True)
env = os.environ.copy()
env['PYINSTALLER_RESET_ENVIRONMENT'] = '1'
# Test the critical fallback: the intro must work without the bootloader splash.
env['PYINSTALLER_SUPPRESS_SPLASH_SCREEN'] = '1'
p = subprocess.Popen([str(Path('dist/SchoolHub.exe').resolve()), '--startup-visual-test', str(out)], env=env)

def wait_stage(stage):
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        if p.poll() is not None:
            raise AssertionError(f'App exited before {stage}: {p.returncode}')
        try:
            result = json.loads(out.read_text())
            if result['stage'] == stage:
                return result
        except (FileNotFoundError, json.JSONDecodeError, PermissionError):
            pass
        time.sleep(0.05)
    raise AssertionError(f'App never showed {stage}')

try:
    intro = wait_stage('intro')
    assert abs(intro['x'] - (intro['screen_width'] - intro['width']) / 2) <= 3, intro
    assert abs(intro['y'] - (intro['screen_height'] - intro['height']) / 2) <= 3, intro
    first = ImageGrab.grab(window=intro['hwnd']).convert('RGB')
    first.save('dist/startup-intro-1.png')
    time.sleep(0.65)
    second = ImageGrab.grab(window=intro['hwnd']).convert('RGB')
    second.save('dist/startup-intro-2.png')
    assert first.size == second.size and first.width >= 600, first.size
    colors = list(first.getdata())
    white = sum(min(rgb) > 235 for rgb in colors) / len(colors)
    assert white < 0.15, f'White startup window: {white:.1%}'
    blue = sum(b > 100 and b > r * 1.4 for r, g, b in colors)
    assert blue > 1000, 'Intro artwork not rendered'
    assert ImageChops.difference(first, second).getbbox(), 'Intro is static, not animated'
    ready = wait_stage('ready')
    time.sleep(0.2)
    ImageGrab.grab(window=ready['hwnd']).save('dist/startup-ready.png')
    assert ready['width'] >= 720 and ready['height'] >= 520, ready
    print('PASS: centered visible intro, dark first frame, moving animation, main window ready')
finally:
    subprocess.run(['taskkill', '/PID', str(p.pid), '/T', '/F'], capture_output=True)
