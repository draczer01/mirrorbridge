# PyInstaller spec for the standalone MirrorBridge.exe (one file, FFmpeg included).
# Build on Windows:  pyinstaller --noconfirm --clean packaging/MirrorBridge.spec
import os
import shutil

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

ROOT = os.path.abspath(os.path.join(SPECPATH, ".."))

# FFmpeg: MIRRORBRIDGE_FFMPEG=C:\path\ffmpeg.exe (or AIRCAST_FFMPEG, its name before the rename),
# or the build from the imageio-ffmpeg wheel (FFmpeg 7.1 with ddagrab, NVENC, Quick Sync, AMF and
# Media Foundation).
ffmpeg_src = os.environ.get("MIRRORBRIDGE_FFMPEG") or os.environ.get("AIRCAST_FFMPEG")
if not ffmpeg_src:
    import imageio_ffmpeg

    ffmpeg_src = imageio_ffmpeg.get_ffmpeg_exe()
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
