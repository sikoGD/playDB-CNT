@echo off
call "C:\Program Files (x86)\Microsoft Visual Studio\2022\BuildTools\VC\Auxiliary\Build\vcvars64.bat" >nul
cd /d "%~dp0"
cl /nologo /O2 /MT /W4 /LD dusk_online.c /Fe:version.dll /link /DEF:version.def ws2_32.lib
