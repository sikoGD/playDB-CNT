ooz: the open-source Kraken decompressor "ooz" by Powzix (https://github.com/powzix/ooz),
GPL-3.0-or-later (see COPYING.txt). The launcher uses it to read the game's menu text so it can add
the Bot Match name and description. It runs as a separate program.

Changes for playDB-CNT (diff: source/playdb-changes.diff):
- Kraken blocks with the "restart" flag are decoded as independent blocks (needed for the game's
  newer Oodle files).
- A minimal command line: ooz <raw Kraken stream> <unpacked size> <output file>.
- stdafx.h builds with GCC/Clang on macOS and Linux; on ARM the SSE code runs through sse2neon.h
  (MIT, see source/sse2neon-LICENSE).

Build:
  Windows (Visual Studio x64 prompt):
    cl /O2 /MT /EHsc /D_CRT_SECURE_NO_WARNINGS kraken.cpp bitknit.cpp lzna.cpp /Fe:ooz.exe
  macOS / Linux (x86-64):
    c++ -O2 -msse4.1 -w kraken.cpp bitknit.cpp lzna.cpp -o ooz
  macOS (Apple Silicon) / Linux (ARM):
    c++ -O2 -w kraken.cpp bitknit.cpp lzna.cpp -o ooz
The release packages for macOS and Linux include a prebuilt ooz (.github/workflows/packages.yml).
