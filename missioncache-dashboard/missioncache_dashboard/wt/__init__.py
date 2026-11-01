"""The Windows Terminal panels: Recent Sessions and Saved Tabs beside Windows
Terminal, served by ``daemon`` (``missioncache-wt``), which reads the
Terminal over UI Automation (``uia``, Windows only)."""

# Each installed Windows Terminal (stable, Preview) under %LOCALAPPDATA%\Microsoft\WindowsApps.
WT_EXE_GLOB = "Microsoft.WindowsTerminal*_8wekyb3d8bbwe/wt.exe"
