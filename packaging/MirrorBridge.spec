# PyInstaller spec for the standalone MirrorBridge.exe (one file, FFmpeg included).
# Build on Windows with packaging\build_exe.ps1, which fetches and checks FFmpeg first. By hand:
#   python -I packaging/fetch_ffmpeg.py
#   pyinstaller --noconfirm --clean packaging/MirrorBridge.spec
import os
import shutil
import sys

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

ROOT = os.path.abspath(os.path.join(SPECPATH, ".."))
sys.path.insert(0, SPECPATH)
import fetch_ffmpeg  # noqa: E402

# FFmpeg: the build packaging/ffmpeg_pin.json names (gyan.dev's FFmpeg 8.1.1 essentials: ddagrab,
# scale_d3d11 to scale on the GPU, NVENC, Quick Sync, AMF and Media Foundation), as fetch_ffmpeg.py
# checked it into build/ffmpeg_811, and nothing else, even when MIRRORBRIDGE_FFMPEG is set. Only
# MIRRORBRIDGE_OTHER_FFMPEG=1 (build_exe.ps1 -AllowOtherFFmpeg) brings back the old choice:
# MIRRORBRIDGE_FFMPEG=C:\path\ffmpeg.exe (or AIRCAST_FFMPEG, its name before the rename), or else
# the imageio-ffmpeg wheel's FFmpeg 7.1.
ffmpeg_src, ffmpeg_why = fetch_ffmpeg.bundled()
print(f"FFmpeg inside MirrorBridge.exe: {ffmpeg_src} ({ffmpeg_why})")
stage = os.path.join(ROOT, "build", "ffmpeg_stage")
os.makedirs(stage, exist_ok=True)
shutil.copyfile(ffmpeg_src, os.path.join(stage, "ffmpeg.exe"))  # the app looks for ffmpeg\ffmpeg.exe

hidden = collect_submodules("pyatv") + collect_submodules("zeroconf") + ["soundcard", "soundcard.mediafoundation"]
# Extend screen (normal imports reach these too; listed so the bundle can never miss them).
hidden += ["mirrorbridge.vdisplay", "mirrorbridge.extended", "mirrorbridge.gui.extend"]
# Screen mirroring (experimental): Mirror screen, Test this TV and the mirror command.
hidden += collect_submodules("mirrorbridge.mirror") + collect_submodules("mirrorbridge.googlecast")
datas = collect_data_files("pyatv") + collect_data_files("soundcard")

a = Analysis(
    [os.path.join(SPECPATH, "mirrorbridge_entry.py")],
    pathex=[ROOT],
    binaries=[(os.path.join(stage, "ffmpeg.exe"), "ffmpeg")],
    datas=datas,
    hiddenimports=hidden,
    excludes=["tkinter", "imageio_ffmpeg", "matplotlib", "PIL", "pytest", "IPython"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="MirrorBridge",
    console=False,
    icon=os.path.join(SPECPATH, "mirrorbridge.ico"),
    version=os.path.join(SPECPATH, "version_info.txt"),
    upx=False,
)
