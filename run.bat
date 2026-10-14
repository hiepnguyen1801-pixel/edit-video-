@echo off
REM One-click batch run (Windows). Put videos in input\, then double-click this file.
cd /d "%~dp0"
for %%d in (input output music broll stickers) do if not exist %%d mkdir %%d
python tiktok_batch.py -i input -o output -m music -b broll -s stickers -j 2 %*
pause
