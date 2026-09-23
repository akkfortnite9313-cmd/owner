# Сборка ClassBot.exe: pyinstaller ClassBot.spec
# Браузер в сборку не входит — бот использует установленный Chrome или Edge.
from PyInstaller.utils.hooks import collect_all, collect_data_files

datas = [("assets", "assets")]
binaries = []
hiddenimports = []
pw_datas, pw_binaries, pw_hidden = collect_all("playwright")
datas += pw_datas + collect_data_files("sv_ttk")
binaries += pw_binaries
hiddenimports += pw_hidden + ["sv_ttk"]

a = Analysis(
    ["main.py"],
    datas=datas,
    binaries=binaries,
    hiddenimports=hiddenimports,
    excludes=["pytest"],
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="ClassBot",
    icon="assets/icon.ico",
    console=False,
    upx=False,
)
