playDB-CNT online patch (version.dll)
=====================================

For Windows emulators that cannot run The Duskbloods Network Test online by themselves (stock
Lichii, Eden): the launcher's "Patch Lichii" button copies this version.dll next to the emulator,
and "Remove patch" deletes it again.

The emulator loads it at start. It forwards every real version.dll function to the Windows copy
and, only when the launcher starts the game (EDEN_NX2_ONLINE_HOSTS is set), does what the
Duskbloods emulator builds do:
  - the game's online names resolve to 127.0.0.1 (the emulator's own block of Nintendo names is
    lifted for those names only),
  - connections to 127.0.0.1:443 go to the launcher's server (EDEN_NX2_ONLINE_SERVER),
  - offline, nothing but this PC is reachable (EDEN_NX2_LOOPBACK_ONLY).
It hooks getaddrinfo, gethostbyname, connect and sendto in the emulator program only, and writes
online_shim.log into the session's log folder.

Started without the launcher, the emulator runs exactly as before.

Build (Visual Studio Build Tools, x64): source\build.bat
