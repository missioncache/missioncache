"""Two iTerm2 Toolbelt panels for the resume lists: Recent Sessions (every
live iTerm2 session, the one that changed last on top) and Saved Tabs (the
Saved and Closed lists, with Open). A small daemon serves both, because
focusing a tab, opening one and the key-binding functions all need the
iTerm2 Python API. Mac only, installed with ``missioncache-install
--iterm-panels``. ``logic`` holds the parts that need no iTerm2."""
