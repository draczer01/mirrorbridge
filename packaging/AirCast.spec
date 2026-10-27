# PyInstaller spec for the standalone AirCast.exe (one file, FFmpeg included).
# Build on Windows:  pyinstaller --noconfirm --clean packaging/AirCast.spec
import os
import shutil

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

ROOT = os.path.abspath(os.path.join(SPECPATH, ".."))

# FFmpeg: AIRCAST_FFMPEG=C:\path\ffmpeg.exe, or the build from the imageio-ffmpeg wheel
# (FFmpeg 7.1 with ddagrab, NVENC, Quick Sync, AMF and Media Foundation).
ffmpeg_src = os.environ.get("AIRCAST_FFMPEG")
if not ffmpeg_src:
    import imageio_ffmpeg

    ffmpeg_src = imageio_ffmpeg.get_ffmpeg_exe()
stage = os.path.join(ROOT, "build", "ffmpeg_stage")
os.makedirs(stage, exist_ok=True)
shutil.copyfile(ffmpeg_src, os.path.join(stage, "ffmpeg.exe"))  # the app looks for ffmpeg\ffmpeg.exe

hidden = collect_submodules("pyatv") + collect_submodules("zeroconf") + ["soundcard", "soundcard.mediafoundation"]
datas = collect_data_files("pyatv") + collect_data_files("soundcard")

a = Analysis(
    [os.path.join(SPECPATH, "aircast_entry.py")],
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
    name="AirCast",
    console=False,
    icon=os.path.join(SPECPATH, "aircast.ico"),
    version=os.path.join(SPECPATH, "version_info.txt"),
    upx=False,
)
